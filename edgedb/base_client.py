#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import abc
import typing

from . import abstract
from . import con_utils
from . import errors
from . import options
from .protocol import protocol


BaseConnection_T = typing.TypeVar('BaseConnection_T', bound='BaseConnection')


class BaseConnection(metaclass=abc.ABCMeta):
    _protocol: typing.Any
    _addr: typing.Optional[typing.Union[str, typing.Tuple[str, int]]]
    _addrs: typing.Iterable[typing.Union[str, typing.Tuple[str, int]]]
    _config: con_utils.ClientConfiguration
    _params: con_utils.ResolvedConnectConfig
    _log_listeners: typing.Set[
        typing.Callable[[BaseConnection_T, errors.EdgeDBMessage], None]
    ]
    __slots__ = (
        "__weakref__",
        "_protocol",
        "_addr",
        "_addrs",
        "_config",
        "_params",
        "_log_listeners",
        "_holder",
    )

    def __init__(
        self,
        addrs: typing.Iterable[typing.Union[str, typing.Tuple[str, int]]],
        config: con_utils.ClientConfiguration,
        params: con_utils.ResolvedConnectConfig,
    ):
        self._addr = None
        self._protocol = None
        self._addrs = addrs
        self._config = config
        self._params = params
        self._log_listeners = set()
        self._holder = None

    @abc.abstractmethod
    def _dispatch_log_message(self, msg):
        ...

    def _on_log_message(self, msg):
        if self._log_listeners:
            self._dispatch_log_message(msg)

    def connected_addr(self):
        return self._addr

    def _get_last_status(self) -> typing.Optional[str]:
        if self._protocol is None:
            return None
        status = self._protocol.last_status
        if status is not None:
            status = status.decode()
        return status

    def _cleanup(self):
        self._log_listeners.clear()
        if self._holder:
            self._holder._release_on_close()
            self._holder = None

    def add_log_listener(
        self: BaseConnection_T,
        callback: typing.Callable[[BaseConnection_T, errors.EdgeDBMessage],
                                  None]
    ) -> None:
        """Add a listener for EdgeDB log messages.

        :param callable callback:
            A callable receiving the following arguments:
            **connection**: a Connection the callback is registered with;
            **message**: the `edgedb.EdgeDBMessage` message.
        """
        self._log_listeners.add(callback)

    def remove_log_listener(
        self: BaseConnection_T,
        callback: typing.Callable[[BaseConnection_T, errors.EdgeDBMessage],
                                  None]
    ) -> None:
        """Remove a listening callback for log messages."""
        self._log_listeners.discard(callback)

    @property
    def dbname(self) -> str:
        return self._params.database

    @abc.abstractmethod
    def is_closed(self) -> bool:
        ...

    def is_in_transaction(self) -> bool:
        """Return True if Connection is currently inside a transaction.

        :return bool: True if inside transaction, False otherwise.
        """
        return self._protocol.is_in_transaction()

    def get_settings(self) -> typing.Dict[str, typing.Any]:
        return self._protocol.get_settings()

    def terminate(self):
        if not self.is_closed():
            try:
                self._protocol.abort()
            finally:
                self._cleanup()


class BaseImpl(abc.ABC):
    __slots__ = (
        "_connect_args",
        "_codecs_registry",
        "_query_cache",
        "_connection_class",
        "_on_connect",
        "_on_acquire",
        "_on_release",
    )

    def __init__(
        self,
        connect_args,
        *,
        connection_class,
        on_connect,
        on_acquire,
        on_release,
    ):
        if not issubclass(connection_class, BaseConnection):
            raise TypeError(
                f'connection_class is expected to be a subclass of '
                f'edgedb.base_client.BaseConnection, '
                f'got {connection_class}')
        self._connection_class = connection_class
        self._connect_args = connect_args
        self._on_connect = on_connect
        self._on_acquire = on_acquire
        self._on_release = on_release
        self._codecs_registry = protocol.CodecsRegistry()
        self._query_cache = protocol.QueryCodecsCache()

    def _parse_connect_args(self):
        return con_utils.parse_connect_arguments(
            **self._connect_args,
            # ToDos
            command_timeout=None,
            server_settings=None,
        )

    @abc.abstractmethod
    def get_concurrency(self):
        ...

    @abc.abstractmethod
    def get_free_size(self):
        ...

    @abc.abstractmethod
    async def ensure_connected(self):
        ...

    def set_connect_args(self, dsn=None, **connect_kwargs):
        r"""Set the new connection arguments for this pool.

        The new connection arguments will be used for all subsequent
        new connection attempts.  Existing connections will remain until
        they expire. Use AsyncIOPool.expire_connections() to expedite
        the connection expiry.

        :param str dsn:
            Connection arguments specified using as a single string in
            the following format:
            ``edgedb://user:pass@host:port/database?option=value``.

        :param \*\*connect_kwargs:
            Keyword arguments for the
            :func:`~edgedb.asyncio_client.create_async_client` function.
        """

        connect_kwargs["dsn"] = dsn
        self._connect_args = connect_kwargs
        self._codecs_registry = protocol.CodecsRegistry()
        self._query_cache = protocol.QueryCodecsCache()

    @property
    def codecs_registry(self):
        return self._codecs_registry

    @property
    def query_cache(self):
        return self._query_cache


class PoolConnectionHolder(abc.ABC):
    __slots__ = (
        "_con",
        "_pool",
        "_on_acquire",
        "_on_release",
        "_release_event",
        "_timeout",
        "_generation",
    )

    def __init__(self, pool, *, on_acquire, on_release):

        self._pool = pool
        self._con = None

        self._on_acquire = on_acquire
        self._on_release = on_release
        self._timeout = None
        self._generation = None

    @abc.abstractmethod
    async def close(self, *, wait=True):
        ...

    @abc.abstractmethod
    async def wait_until_released(self, timeout=None):
        ...

    async def connect(self):
        if self._con is not None:
            raise errors.InternalClientError(
                'PoolConnectionHolder.connect() called while another '
                'connection already exists')

        self._con = await self._pool._get_new_connection()
        assert self._con._holder is None
        self._con._holder = self
        self._generation = self._pool._generation

    async def acquire(self) -> BaseConnection:
        if self._con is None or self._con.is_closed():
            self._con = None
            await self.connect()

        elif self._generation != self._pool._generation:
            # Connections have been expired, re-connect the holder.
            await self.close(wait=False)
            self._con = None
            await self.connect()

        if self._on_acquire is not None:
            await self._pool._callback(self._on_acquire, self._con)

        self._release_event.clear()

        return self._con

    async def release(self, timeout):
        if self._release_event.is_set():
            raise errors.InternalClientError(
                'PoolConnectionHolder.release() called on '
                'a free connection holder')

        if self._con.is_closed():
            # This is usually the case when the connection is broken rather
            # than closed by the user, so we need to call _release_on_close()
            # here to release the holder back to the queue, because
            # self._con._cleanup() was never called. On the other hand, it is
            # safe to call self._release() twice - the second call is no-op.
            self._release_on_close()
            return

        self._timeout = None

        if self._generation != self._pool._generation:
            # The connection has expired because it belongs to
            # an older generation (AsyncIOPool.expire_connections() has
            # been called.)
            await self.close()
            return

        if self._on_release is not None:
            await self._pool._callback(self._on_release, self._con)

        # Free this connection holder and invalidate the
        # connection proxy.
        self._release()

    def terminate(self):
        if self._con is not None:
            # AsyncIOConnection.terminate() will call _release_on_close() to
            # finish holder cleanup.
            self._con.terminate()

    def _release_on_close(self):
        self._release()
        self._con = None

    def _release(self):
        """Release this connection holder."""
        if self._release_event.is_set():
            # The holder is not checked out.
            return

        self._release_event.set()

        # Put ourselves back to the pool queue.
        self._pool._queue.put_nowait(self)


class BasePoolImpl(BaseImpl, abc.ABC):
    __slots__ = (
        "_queue",
        "_user_concurrency",
        "_concurrency",
        "_first_connect_lock",
        "_working_addr",
        "_working_config",
        "_working_params",
        "_holders",
        "_initialized",
        "_initializing",
        "_closing",
        "_closed",
        "_generation",
    )

    _holder_class = NotImplemented

    def __init__(
        self,
        connect_args,
        *,
        concurrency: typing.Optional[int],
        on_connect=None,
        on_acquire=None,
        on_release=None,
        connection_class,
    ):
        super().__init__(
            connect_args,
            on_connect=on_connect,
            on_acquire=on_acquire,
            on_release=on_release,
            connection_class=connection_class,
        )

        if concurrency is not None and concurrency <= 0:
            raise ValueError('concurrency is expected to be greater than zero')

        self._user_concurrency = concurrency
        self._concurrency = concurrency if concurrency else 1

        self._holders = []
        self._queue = None

        self._first_connect_lock = None
        self._working_addr = None
        self._working_config = None
        self._working_params = None

        self._closing = False
        self._closed = False
        self._generation = 0

    @abc.abstractmethod
    def _ensure_initialized(self):
        ...

    @abc.abstractmethod
    def _set_queue_maxsize(self, maxsize):
        ...

    @abc.abstractmethod
    async def _new_connection_with_params(self, addr, config, params):
        ...

    @abc.abstractmethod
    async def _maybe_get_first_connection(self):
        ...

    @abc.abstractmethod
    async def _callback(self, cb, con):
        ...

    @abc.abstractmethod
    async def acquire(self, timeout=None):
        ...

    @abc.abstractmethod
    async def _release(self, connection):
        ...

    def _resize_holder_pool(self):
        resize_diff = self._concurrency - len(self._holders)

        if (resize_diff > 0):
            if self._queue.maxsize != self._concurrency:
                self._set_queue_maxsize(self._concurrency)

            for _ in range(resize_diff):
                ch = self._holder_class(
                    self,
                    on_acquire=self._on_acquire,
                    on_release=self._on_release)

                self._holders.append(ch)
                self._queue.put_nowait(ch)
        elif resize_diff < 0:
            # TODO: shrink the pool
            pass

    def get_concurrency(self):
        return self._concurrency

    def get_free_size(self):
        if self._queue is None:
            # Queue has not been initialized yet
            return self._concurrency

        return self._queue.qsize()

    def set_connect_args(self, dsn=None, **connect_kwargs):
        super().set_connect_args(dsn=dsn, **connect_kwargs)
        self._working_addr = None
        self._working_config = None
        self._working_params = None

    async def _get_first_connection(self):
        # First connection attempt on this pool.
        connect_config, client_config = self._parse_connect_args()
        con = await self._new_connection_with_params(
            connect_config.address, client_config, connect_config
        )
        self._working_addr = con.connected_addr()
        self._working_config = client_config
        self._working_params = connect_config

        if self._user_concurrency is None:
            suggested_concurrency = con.get_settings().get(
                'suggested_pool_concurrency')
            if suggested_concurrency:
                self._concurrency = suggested_concurrency
                self._resize_holder_pool()
        return con

    async def _get_new_connection(self):
        con = None
        if self._working_addr is None:
            con = await self._maybe_get_first_connection()
        if con is None:
            assert self._working_addr is not None
            # We've connected before and have a resolved address,
            # and parsed options and config.
            con = await self._new_connection_with_params(
                self._working_addr,
                self._working_config,
                self._working_params,
            )

        if self._on_connect is not None:
            await self._callback(self._on_connect, con)

        return con

    async def release(self, connection):

        if not isinstance(connection, BaseConnection):
            raise errors.InterfaceError(
                f'BasePoolImpl.release() received invalid connection: '
                f'{connection!r} does not belong to any connection pool'
            )

        ch = connection._holder
        if ch is None:
            # Already released, do nothing.
            return

        if ch._pool is not self:
            raise errors.InterfaceError(
                f'BasePoolImpl.release() received invalid connection: '
                f'{connection!r} is not a member of this pool'
            )

        return await self._release(ch)

    def terminate(self):
        """Terminate all connections in the pool."""
        if self._closed:
            return
        for ch in self._holders:
            ch.terminate()
        self._closed = True

    async def expire_connections(self):
        self._generation += 1

    async def ensure_connected(self):
        self._ensure_initialized()

        for ch in self._holders:
            if ch._con is not None and not ch._con.is_closed():
                return

        ch = self._holders[0]
        ch._con = None
        await ch.connect()


class BaseClient(
    abstract.BaseReadOnlyExecutor, options._OptionsMixin, abc.ABC
):
    __slots__ = ("_impl", "_options")
    _impl_class = NotImplemented

    def __init__(
        self,
        *,
        connection_class,
        concurrency: typing.Optional[int],
        dsn=None,
        host: str = None,
        port: int = None,
        credentials: str = None,
        credentials_file: str = None,
        user: str = None,
        password: str = None,
        database: str = None,
        tls_ca: str = None,
        tls_ca_file: str = None,
        tls_security: str = None,
        wait_until_available: int = 30,
        timeout: int = 10,
        on_connect=None,
        on_acquire=None,
        on_release=None,
        **kwargs,
    ):
        super().__init__()
        connect_args = {
            "dsn": dsn,
            "host": host,
            "port": port,
            "credentials": credentials,
            "credentials_file": credentials_file,
            "user": user,
            "password": password,
            "database": database,
            "timeout": timeout,
            "tls_ca": tls_ca,
            "tls_ca_file": tls_ca_file,
            "tls_security": tls_security,
            "wait_until_available": wait_until_available,
        }

        self._impl = self._impl_class(
            connect_args,
            connection_class=connection_class,
            concurrency=concurrency,
            on_connect=on_connect,
            on_acquire=on_acquire,
            on_release=on_release,
            **kwargs,
        )

    def _shallow_clone(self):
        new_client = self.__class__.__new__(self.__class__)
        new_client._impl = self._impl
        return new_client

    def _get_query_cache(self) -> abstract.QueryCache:
        return abstract.QueryCache(
            codecs_registry=self._impl.codecs_registry,
            query_cache=self._impl.query_cache,
        )

    def _get_retry_options(self) -> typing.Optional[options.RetryOptions]:
        return self._options.retry_options

    @property
    def concurrency(self) -> int:
        """Max number of connections in the pool."""

        return self._impl.get_concurrency()

    @property
    def free_size(self) -> int:
        """Number of available connections in the pool."""

        return self._impl.get_free_size()