"""This module implements the Compiler class."""
from __future__ import annotations

import atexit
import functools
import logging
import multiprocessing as mp
import os
import signal
import time
import uuid
import warnings
from multiprocessing.connection import Client
from multiprocessing.connection import Connection
from types import FrameType
from typing import Iterable
from typing import overload
from typing import TYPE_CHECKING

from bqskit.compiler.status import CompilationStatus
from bqskit.compiler.task import CompilationTask
from bqskit.runtime.attached import start_attached_server
from bqskit.runtime.message import RuntimeMessage
from bqskit.utils.typing import is_iterable

if TYPE_CHECKING:
    from typing import Any
    from bqskit.compiler.basepass import BasePass
    from bqskit.ir.circuit import Circuit

_logger = logging.getLogger(__name__)


class Compiler:
    """
    A compiler is responsible for accepting and managing compilation tasks.

    The compiler class either spins up a parallel runtime or connects to
    a distributed one, which compilation tasks can then access to
    parallelize their operations. The compiler is implemented as a
    context manager and it is recommended to use it as one. If the
    compiler is not used in a context manager, it is the responsibility
    of the user to call `close()`.

    Examples:
        1. Use in a context manager:
        >>> with Compiler() as compiler:
        ...     circuit = compiler.compile(task)

        2. Use compiler without context manager:
        >>> compiler = Compiler()
        >>> circuit = compiler.compile(task)
        >>> compiler.close()

        3. Connect to an already running detached runtime:
        >>> with Compiler('localhost', 8786) as compiler:
        ...     circuit = compiler.compile(task)

        4. Start and attach to a runtime with 4 worker processes:
        >>> with Compiler(num_workers=4) as compiler:
        ...     circuit = compiler.compile(task)
    """

    def __init__(
        self,
        ip: None | str = None,
        port: None | int = None,
        num_workers: int = -1,
    ) -> None:
        """Construct a Compiler object."""
        self.p: mp.Process | None = None
        self.conn: Connection | None = None
        if port is None:
            port = 7472

        atexit.register(self.close)
        if ip is None:
            ip = 'localhost'
            self._start_server(num_workers)

        self._connect_to_server(ip, port)

    def _start_server(self, num_workers: int) -> None:
        self.p = mp.Process(target=start_attached_server, args=(num_workers,))
        _logger.debug('Starting runtime server process.')
        self.p.start()

    def _connect_to_server(self, ip: str, port: int) -> None:
        max_retries = 5
        wait_time = .25
        for _ in range(max_retries):
            try:
                conn = Client((ip, port))
            except ConnectionRefusedError:
                time.sleep(wait_time)
                wait_time *= 2
            else:
                self.conn = conn
                self.old_signal = signal.signal(
                    signal.SIGINT, functools.partial(
                        sigint_handler, compiler=self,
                    ),
                )
                if self.conn is None:
                    raise RuntimeError('Connection unexpectedly none.')
                self.conn.send((RuntimeMessage.CONNECT, None))
                _logger.debug('Successfully connected to runtime server.')
                return
        raise RuntimeError('Client connection refused')

    def __enter__(self) -> Compiler:
        """Enter a context for this compiler."""
        return self

    def __exit__(self, type: Any, value: Any, traceback: Any) -> None:
        """Shutdown compiler."""
        self.close()

    def close(self) -> None:
        """Shutdown the compiler."""
        # Disconnect from server
        if self.conn is not None:
            try:
                self.conn.send((RuntimeMessage.DISCONNECT, None))
                self.conn.close()
            except Exception as e:
                _logger.debug(
                    'Unsuccessfully disconnected from runtime server.',
                )
                _logger.debug(e)
            else:
                _logger.debug('Disconnected from runtime server.')
            finally:
                self.conn = None

        # Shutdown server if attached
        if self.p is not None and self.p.pid is not None:
            try:
                os.kill(self.p.pid, signal.SIGINT)
                _logger.debug('Interrupted attached runtime server.')

                self.p.join(1)
                if self.p.exitcode is None:
                    os.kill(self.p.pid, signal.SIGKILL)
                    _logger.debug('Killed attached runtime server.')

            except Exception as e:
                _logger.debug(
                    f'Error while shuting down attached runtime server: {e}.',
                )
            else:
                _logger.debug('Successfully shutdown attached runtime server.')
            finally:
                self.p.join()
                _logger.debug('Attached runtime server is down.')
                self.p = None

        # Reset interrupt signal handler and remove exit handler
        signal.signal(signal.SIGINT, self.old_signal)
        atexit.unregister(self.close)

    def __del__(self) -> None:
        self.close()
        _logger.debug('Compiler successfully shutdown.')

    def submit(
        self,
        task_or_circuit: CompilationTask | Circuit,
        workflow: Iterable[BasePass] | None = None,
        request_data: bool = False,
        logging_level: int | None = None,
        max_logging_depth: int = -1,
    ) -> uuid.UUID:
        """Submit a CompilationTask to the Compiler."""
        # Build CompilationTask
        if isinstance(task_or_circuit, CompilationTask):
            if workflow is not None:
                raise ValueError(
                    'Cannot specify workflow and task.'
                    ' Either specify a workflow and circuit or a task alone.',
                )

            task = task_or_circuit

        else:
            if workflow is None:
                raise TypeError(
                    'Must specify workflow when providing a circuit to submit.',
                )

            if not is_iterable(workflow):
                raise TypeError('Expected sequence of bqskit passes.')

            task = CompilationTask(task_or_circuit, list(workflow))

        # Set task configuration
        task.request_data = request_data
        task.logging_level = logging_level
        task.max_logging_depth = max_logging_depth

        # Submit task to runtime
        self._send(RuntimeMessage.SUBMIT, task)
        return task.task_id

    def status(self, task_id: CompilationTask | uuid.UUID) -> CompilationStatus:
        """Retrieve the status of the specified CompilationTask."""
        if isinstance(task_id, CompilationTask):
            warnings.warn('DEPRECATED...')  # TODO
            task_id = task_id.task_id
        assert isinstance(task_id, uuid.UUID)

        msg, payload = self._send_recv(RuntimeMessage.STATUS, task_id)
        if msg != RuntimeMessage.STATUS:
            raise RuntimeError(f'Unexpected message type: {msg}.')
        return payload

    def result(
        self,
        task_id: CompilationTask | uuid.UUID,
    ) -> Circuit | tuple[Circuit, dict[str, Any]]:
        """Block until the CompilationTask is finished, return its result."""
        if isinstance(task_id, CompilationTask):
            warnings.warn('DEPRECATED...')  # TODO
            task_id = task_id.task_id
        assert isinstance(task_id, uuid.UUID)

        msg, payload = self._send_recv(RuntimeMessage.REQUEST, task_id)
        if msg != RuntimeMessage.RESULT:
            raise RuntimeError(f'Unexpected message type: {msg}.')
        return payload

    def cancel(self, task_id: CompilationTask | uuid.UUID) -> bool:
        """Remove a task from the compiler's workqueue."""
        if isinstance(task_id, CompilationTask):
            warnings.warn('DEPRECATED...')  # TODO
            task_id = task_id.task_id
        assert isinstance(task_id, uuid.UUID)

        msg, _ = self._send_recv(RuntimeMessage.CANCEL, task_id)
        if msg != RuntimeMessage.CANCEL:
            raise RuntimeError(f'Unexpected message type: {msg}.')
        return True

    @overload
    def compile(
        self,
        task_or_circuit: CompilationTask,
    ) -> Circuit | tuple[Circuit, dict[str, Any]]:
        ...

    @overload
    def compile(
        self,
        task_or_circuit: Circuit,
        workflow: Iterable[BasePass],
        request_data: None,
        logging_level: int | None,
        max_logging_depth: int,
    ) -> Circuit:
        ...

    @overload
    def compile(
        self,
        task_or_circuit: Circuit,
        workflow: Iterable[BasePass],
        request_data: bool,
        logging_level: int | None,
        max_logging_depth: int,
    ) -> tuple[Circuit, dict[str, Any]]:
        ...

    def compile(
        self,
        task_or_circuit: CompilationTask | Circuit,
        workflow: Iterable[BasePass] | None = None,
        request_data: bool | None = None,
        logging_level: int | None = None,
        max_logging_depth: int = -1,
    ) -> Circuit | tuple[Circuit, dict[str, Any]]:
        """Submit and execute the CompilationTask, block until its done."""
        if isinstance(task_or_circuit, CompilationTask):
            warnings.warn('DEPRECATED...')  # TODO

        task_id = self.submit(
            task_or_circuit,
            workflow,
            request_data if request_data else False,
            logging_level,
            max_logging_depth,
        )
        result = self.result(task_id)
        return result

    def _send(
        self,
        msg: RuntimeMessage,
        payload: Any,
    ) -> None:
        if self.conn is None:
            raise RuntimeError('Connection unexpectedly none.')

        try:
            self._recv_log_error_until_empty()
            
            self.conn.send((msg, payload))

        except Exception as e:
            self.conn = None
            self.close()
            raise RuntimeError('Server connection unexpectedly closed.') from e

    def _send_recv(
        self,
        msg: RuntimeMessage,
        payload: Any,
    ) -> tuple[RuntimeMessage, Any]:
        if self.conn is None:
            raise RuntimeError('Connection unexpectedly none.')

        try:
            self._recv_log_error_until_empty()

            self.conn.send((msg, payload))

            return self._recv_handle_log_error()

        except Exception as e:
            self.conn = None
            self.close()
            raise RuntimeError('Server connection unexpectedly closed.') from e

    def _recv_handle_log_error(self) -> tuple[RuntimeMessage, Any]:
        """Return next msg, transparently emit log records and raise errors."""
        if self.conn is None:
            raise RuntimeError('Connection unexpectedly none.')

        to_return = None
        while to_return is None or self.conn.poll():
            msg, payload = self.conn.recv()

            if msg == RuntimeMessage.LOG:
                logger = logging.getLogger(payload.name)
                if logger.isEnabledFor(payload.levelno):
                    logger.handle(payload)

            elif msg == RuntimeMessage.ERROR:
                raise RuntimeError(payload)

            else:
                # Communication between runtime server and compiler
                # is always round-trip. Once we have received our
                # desired message (not log or error) we can therefore be
                # certain any remaining messages in the pipeline are
                # only either logs or error messages. We do want to
                # handle these sooner rather than later, so we ensure to
                # process every arrived message before returning.
                # Hence, the `or self.conn.poll()` in the while condition.
                to_return = (msg, payload)

        return to_return
    
    def _recv_log_error_until_empty(self) -> None:
        """Handle all remaining log and error messages in the pipeline."""
        if self.conn is None:
            raise RuntimeError('Connection unexpectedly none.')

        while self.conn.poll():
            msg, payload = self.conn.recv()

            if msg == RuntimeMessage.LOG:
                logger = logging.getLogger(payload.name)
                if logger.isEnabledFor(payload.levelno):
                    logger.handle(payload)

            elif msg == RuntimeMessage.ERROR:
                raise RuntimeError(payload)
            
            else:
                raise RuntimeError(f"Unexpected message type: {msg}.")


def sigint_handler(signum: int, frame: FrameType, compiler: Compiler) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _logger.critical('Compiler interrupted.')
    compiler.close()
    raise KeyboardInterrupt
