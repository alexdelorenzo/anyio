import threading
from functools import partial
from asyncio import iscoroutine
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, contextmanager
from types import TracebackType
from typing import (
    Any, AsyncContextManager, Callable, ContextManager, Coroutine, Dict, Generator, Iterable,
    Optional, Tuple, Type, TypeVar, cast, overload)
from warnings import warn

from ._core import _eventloop
from ._core._eventloop import get_asynclib, get_cancelled_exc_class, threadlocals
from ._core._synchronization import Event
from ._core._tasks import CancelScope, TaskStatus, create_task_group

T_Retval = TypeVar('T_Retval')
T_co = TypeVar('T_co')


def run(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args, **kwargs) -> T_Retval:
    """
    Call a coroutine function from a worker thread.

    :param func: a coroutine function
    :param args: positional arguments for the callable
    :return: the return value of the coroutine function

    """
    try:
        asynclib = threadlocals.current_async_module
    except AttributeError:
        raise RuntimeError('This function can only be run from an AnyIO worker thread')

    func_kwargs = partial(func, **kwargs)
    return asynclib.run_async_from_thread(func_kwargs, *args)


def run_async_from_thread(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args) -> T_Retval:
    warn('run_async_from_thread() has been deprecated, use anyio.from_thread.run() instead',
         DeprecationWarning)
    return run(func, *args)


def run_sync(func: Callable[..., T_Retval], *args, **kwargs) -> T_Retval:
    """
    Call a function in the event loop thread from a worker thread.

    :param func: a callable
    :param args: positional arguments for the callable
    :return: the return value of the callable

    """
    try:
        asynclib = threadlocals.current_async_module
    except AttributeError:
        raise RuntimeError('This function can only be run from an AnyIO worker thread')

    func_kwargs = partial(func, **kwargs)
    return asynclib.run_sync_from_thread(func_kwargs, *args)


def run_sync_from_thread(func: Callable[..., T_Retval], *args) -> T_Retval:
    warn('run_sync_from_thread() has been deprecated, use anyio.from_thread.run_sync() instead',
         DeprecationWarning)
    return run_sync(func, *args)


class _BlockingAsyncContextManager(AbstractContextManager):
    _enter_future: Future
    _exit_future: Future
    _exit_event: Event
    _exit_exc_info: Tuple[Optional[Type[BaseException]], Optional[BaseException],
                          Optional[TracebackType]]

    def __init__(self, async_cm: AsyncContextManager[T_co], portal: 'BlockingPortal'):
        self._async_cm = async_cm
        self._portal = portal

    async def run_async_cm(self):
        try:
            self._exit_event = Event()
            value = await self._async_cm.__aenter__()
        except BaseException as exc:
            self._enter_future.set_exception(exc)
            raise
        else:
            self._enter_future.set_result(value)

        await self._exit_event.wait()
        return await self._async_cm.__aexit__(*self._exit_exc_info)

    def __enter__(self) -> T_co:
        self._enter_future = Future()
        self._exit_future = self._portal.start_task_soon(self.run_async_cm)
        cm = self._enter_future.result()
        return cast(T_co, cm)

    def __exit__(self, __exc_type: Optional[Type[BaseException]],
                 __exc_value: Optional[BaseException],
                 __traceback: Optional[TracebackType]) -> Optional[bool]:
        self._exit_exc_info = __exc_type, __exc_value, __traceback
        self._portal.call(self._exit_event.set)
        return self._exit_future.result()


class _BlockingPortalTaskStatus(TaskStatus):
    def __init__(self, future: Future):
        self._future = future

    def started(self, value=None) -> None:
        self._future.set_result(value)


class BlockingPortal:
    """An object that lets external threads run code in an asynchronous event loop."""

    def __new__(cls):
        return get_asynclib().BlockingPortal()

    def __init__(self):
        self._event_loop_thread_id = threading.get_ident()
        self._stop_event = Event()
        self._task_group = create_task_group()
        self._cancelled_exc_class = get_cancelled_exc_class()

    async def __aenter__(self) -> 'BlockingPortal':
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return await self._task_group.__aexit__(exc_type, exc_val, exc_tb)

    def _check_running(self) -> None:
        if self._event_loop_thread_id is None:
            raise RuntimeError('This portal is not running')
        if self._event_loop_thread_id == threading.get_ident():
            raise RuntimeError('This method cannot be called from the event loop thread')

    async def sleep_until_stopped(self) -> None:
        """Sleep until :meth:`stop` is called."""
        await self._stop_event.wait()

    async def stop(self, cancel_remaining: bool = False) -> None:
        """
        Signal the portal to shut down.

        This marks the portal as no longer accepting new calls and exits from
        :meth:`sleep_until_stopped`.

        :param cancel_remaining: ``True`` to cancel all the remaining tasks, ``False`` to let them
            finish before returning

        """
        self._event_loop_thread_id = None
        self._stop_event.set()
        if cancel_remaining:
            self._task_group.cancel_scope.cancel()

    async def _call_func(self, func: Callable, args: tuple, kwargs: Dict[str, Any],
                         future: Future) -> None:
        def callback(f: Future):
            if f.cancelled():
                self.call(scope.cancel)

        try:
            retval = func(*args, **kwargs)
            if iscoroutine(retval):
                with CancelScope() as scope:
                    if future.cancelled():
                        scope.cancel()
                    else:
                        future.add_done_callback(callback)

                    retval = await retval
        except self._cancelled_exc_class:
            future.cancel()
        except BaseException as exc:
            if not future.cancelled():
                future.set_exception(exc)

            # Let base exceptions fall through
            if not isinstance(exc, Exception):
                raise
        else:
            if not future.cancelled():
                future.set_result(retval)
        finally:
            scope = None

    def _spawn_task_from_thread(self, func: Callable, args: tuple, kwargs: Dict[str, Any],
                                name, future: Future) -> None:
        """
        Spawn a new task using the given callable.

        Implementors must ensure that the future is resolved when the task finishes.

        :param func: a callable
        :param args: positional arguments to be passed to the callable
        :param kwargs: keyword arguments to be passed to the callable
        :param name: name of the task (will be coerced to a string if not ``None``)
        :param future: a future that will resolve to the return value of the callable, or the
            exception raised during its execution

        """
        raise NotImplementedError

    @overload
    def call(self, func: Callable[..., Coroutine[Any, Any, T_Retval]], *args) -> T_Retval:
        ...

    @overload
    def call(self, func: Callable[..., T_Retval], *args) -> T_Retval:
        ...

    def call(self, func, *args):
        """
        Call the given function in the event loop thread.

        If the callable returns a coroutine object, it is awaited on.

        :param func: any callable
        :raises RuntimeError: if the portal is not running or if this method is called from within
            the event loop thread

        """
        return self.start_task_soon(func, *args).result()

    def spawn_task(self, func: Callable[..., Coroutine], *args, name=None) -> Future:
        """
        Start a task in the portal's task group.

        :param func: the target coroutine function
        :param args: positional arguments passed to ``func``
        :param name: name of the task (will be coerced to a string if not ``None``)
        :return: a future that resolves with the return value of the callable if the task completes
            successfully, or with the exception raised in the task
        :raises RuntimeError: if the portal is not running or if this method is called from within
            the event loop thread

        .. versionadded:: 2.1
        .. deprecated:: 3.0
           Use :meth:`start_task_soon` instead. If your code needs AnyIO 2 compatibility, you
           can keep using this until AnyIO 4.

        """
        warn('spawn_task() is deprecated -- use start_task_soon() instead', DeprecationWarning)
        return self.start_task_soon(func, *args, name=name)

    def start_task_soon(self, func: Callable[..., Coroutine], *args, name=None) -> Future:
        """
        Start a task in the portal's task group.

        The task will be run inside a cancel scope which can be cancelled by cancelling the
        returned future.

        :param func: the target coroutine function
        :param args: positional arguments passed to ``func``
        :param name: name of the task (will be coerced to a string if not ``None``)
        :return: a future that resolves with the return value of the callable if the task completes
            successfully, or with the exception raised in the task
        :raises RuntimeError: if the portal is not running or if this method is called from within
            the event loop thread

        .. versionadded:: 3.0

        """
        self._check_running()
        f: Future = Future()
        self._spawn_task_from_thread(func, args, {}, name, f)
        return f

    def start_task(self, func: Callable[..., Coroutine], *args, name=None) -> Tuple[Future, Any]:
        """
        Start a task in the portal's task group and wait until it signals for readiness.

        This method works the same way as :meth:`TaskGroup.start`.

        :param func: the target coroutine function
        :param args: positional arguments passed to ``func``
        :param name: name of the task (will be coerced to a string if not ``None``)
        :return: a tuple of (future, task_status_value) where the ``task_status_value`` is the
            value passed to ``task_status.started()`` from within the target function

        .. versionadded:: 3.0

        """
        def task_done(future: Future) -> None:
            if not task_status_future.done():
                if future.cancelled():
                    task_status_future.cancel()
                elif future.exception():
                    task_status_future.set_exception(future.exception())
                else:
                    exc = RuntimeError('Task exited without calling task_status.started()')
                    task_status_future.set_exception(exc)

        self._check_running()
        task_status_future: Future = Future()
        task_status = _BlockingPortalTaskStatus(task_status_future)
        f: Future = Future()
        f.add_done_callback(task_done)
        self._spawn_task_from_thread(func, args, {'task_status': task_status}, name, f)
        return f, task_status_future.result()

    def wrap_async_context_manager(self, cm: AsyncContextManager[T_co]) -> ContextManager[T_co]:
        """
        Wrap an async context manager as a synchronous context manager via this portal.

        Spawns a task that will call both ``__aenter__()`` and ``__aexit__()``, stopping in the
        middle until the synchronous context manager exits.

        :param cm: an asynchronous context manager
        :return: a synchronous context manager

        .. versionadded:: 2.1

        """
        return _BlockingAsyncContextManager(cm, self)


def create_blocking_portal() -> BlockingPortal:
    """
    Create a portal for running functions in the event loop thread from external threads.

    Use this function in asynchronous code when you need to allow external threads access to the
    event loop where your asynchronous code is currently running.

    .. deprecated:: 3.0
        Use :class:`.BlockingPortal` directly.

    """
    warn('create_blocking_portal() has been deprecated -- use anyio.from_thread.BlockingPortal() '
         'directly', DeprecationWarning)
    return BlockingPortal()


@contextmanager
def start_blocking_portal(
        backend: str = 'asyncio',
        backend_options: Optional[Dict[str, Any]] = None) -> Generator[BlockingPortal, Any, None]:
    """
    Start a new event loop in a new thread and run a blocking portal in its main task.

    The parameters are the same as for :func:`~anyio.run`.

    :param backend: name of the backend
    :param backend_options: backend options
    :return: a context manager that yields a blocking portal

    .. versionchanged:: 3.0
        Usage as a context manager is now required.

    """
    async def run_portal():
        async with BlockingPortal() as portal_:
            if future.set_running_or_notify_cancel():
                future.set_result(portal_)
                await portal_.sleep_until_stopped()

    future: Future[BlockingPortal] = Future()
    with ThreadPoolExecutor(1) as executor:
        run_future = executor.submit(_eventloop.run, run_portal, backend=backend,
                                     backend_options=backend_options)
        try:
            wait(cast(Iterable[Future], [run_future, future]), return_when=FIRST_COMPLETED)
        except BaseException:
            future.cancel()
            run_future.cancel()
            raise

        if future.done():
            portal = future.result()
            try:
                yield portal
            except BaseException:
                portal.call(portal.stop, True)
                raise

            portal.call(portal.stop, False)

        run_future.result()
