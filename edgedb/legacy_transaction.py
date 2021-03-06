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
"""
This module provides support for legacy `connection.transaction()` method.

This API is deprecated and will eventually be removed.
Use `retrying_transaction()` or `raw_transaction()` instead.
"""

import enum

from . import errors


__all__ = ('Transaction', 'AsyncIOTransaction')


class TransactionState(enum.Enum):
    NEW = 0
    STARTED = 1
    COMMITTED = 2
    ROLLEDBACK = 3
    FAILED = 4


ISOLATION_LEVELS = {'serializable', 'repeatable_read'}


class BaseTransaction:

    __slots__ = ('_connection', '_isolation', '_readonly', '_deferrable',
                 '_state', '_nested', '_id', '_managed')

    def __init__(self, connection, isolation: str,
                 readonly: bool, deferrable: bool):
        if isolation is not None and isolation not in ISOLATION_LEVELS:
            raise ValueError(
                'isolation is expected to be either of {}, '
                'got {!r}'.format(ISOLATION_LEVELS, isolation))

        self._connection = connection
        self._isolation = isolation
        self._readonly = readonly
        self._deferrable = deferrable
        self._state = TransactionState.NEW
        self._nested = False
        self._id = None
        self._managed = False

    def is_active(self) -> bool:
        return self._state is TransactionState.STARTED

    def __check_state_base(self, opname):
        if self._state is TransactionState.COMMITTED:
            raise errors.InterfaceError(
                'cannot {}; the transaction is already committed'.format(
                    opname))
        if self._state is TransactionState.ROLLEDBACK:
            raise errors.InterfaceError(
                'cannot {}; the transaction is already rolled back'.format(
                    opname))
        if self._state is TransactionState.FAILED:
            raise errors.InterfaceError(
                'cannot {}; the transaction is in error state'.format(
                    opname))

    def __check_state(self, opname):
        if self._state is not TransactionState.STARTED:
            if self._state is TransactionState.NEW:
                raise errors.InterfaceError(
                    'cannot {}; the transaction is not yet started'.format(
                        opname))
            self.__check_state_base(opname)

    def _make_start_query(self):
        self.__check_state_base('start')
        if self._state is TransactionState.STARTED:
            raise errors.InterfaceError(
                'cannot start; the transaction is already started')

        con = self._connection_inner

        if con._top_xact is None:
            con._top_xact = self
        else:
            # Nested transaction block
            top_xact = con._top_xact
            if self._isolation is None:
                self._isolation = top_xact._isolation
            if self._readonly is None:
                self._readonly = top_xact._readonly
            if self._deferrable is None:
                self._deferrable = top_xact._deferrable

            if self._isolation != top_xact._isolation:
                raise errors.InterfaceError(
                    'nested transaction has a different isolation level: '
                    'current {!r} != outer {!r}'.format(
                        self._isolation, top_xact._isolation))

            if self._readonly != top_xact._readonly:
                raise errors.InterfaceError(
                    'nested transaction has a different read-write spec: '
                    'current {!r} != outer {!r}'.format(
                        self._readonly, top_xact._readonly))

            if self._deferrable != top_xact._deferrable:
                raise errors.InterfaceError(
                    'nested transaction has a different deferrable spec: '
                    'current {!r} != outer {!r}'.format(
                        self._deferrable, top_xact._deferrable))

            self._nested = True

        if self._nested:
            self._id = con._get_unique_id('savepoint')
            query = f'DECLARE SAVEPOINT {self._id};'
        else:
            query = 'START TRANSACTION'

            if self._isolation == 'repeatable_read':
                query = 'START TRANSACTION ISOLATION REPEATABLE READ'
            elif self._isolation == 'serializable':
                query = 'START TRANSACTION ISOLATION SERIALIZABLE'

            if self._readonly:
                query += ' READ ONLY'
            elif self._readonly is not None:
                query += ' READ WRITE'
            if self._deferrable:
                query += ' DEFERRABLE'
            elif self._deferrable is not None:
                query += ' NOT DEFERRABLE'
            query += ';'

        return query

    def _make_commit_query(self):
        self.__check_state('commit')

        if self._connection_inner._top_xact is self:
            self._connection_inner._top_xact = None

        if self._nested:
            query = f'RELEASE SAVEPOINT {self._id};'
        else:
            query = 'COMMIT;'

        return query

    def _make_rollback_query(self):
        self.__check_state('rollback')

        if self._connection_inner._top_xact is self:
            self._connection_inner._top_xact = None

        if self._nested:
            query = f'ROLLBACK TO SAVEPOINT {self._id};'
        else:
            query = 'ROLLBACK;'

        return query

    def __repr__(self):
        attrs = []
        attrs.append('state:{}'.format(self._state.name.lower()))

        if self._isolation:
            attrs.append(self._isolation)
        if self._readonly:
            attrs.append('readonly')
        if self._deferrable:
            attrs.append('deferrable')

        if self.__class__.__module__.startswith('edgedb.'):
            mod = 'edgedb'
        else:
            mod = self.__class__.__module__

        return '<{}.{} {} {:#x}>'.format(
            mod, self.__class__.__name__, ' '.join(attrs), id(self))


class AsyncIOTransaction(BaseTransaction):

    async def __aenter__(self):
        if self._managed:
            raise errors.InterfaceError(
                'cannot enter context: already in an `async with` block')
        self._managed = True
        await self.start()

    async def __aexit__(self, extype, ex, tb):
        try:
            if extype is not None:
                await self.__rollback()
            else:
                await self.__commit()
        finally:
            self._managed = False

    async def start(self) -> None:
        """Enter the transaction or savepoint block."""
        await self._connection.ensure_connected()
        self._connection_inner = self._connection._inner
        self._connection_impl = self._connection_inner._impl

        query = self._make_start_query()
        try:
            await self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.STARTED

    async def __commit(self):
        query = self._make_commit_query()
        try:
            await self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.COMMITTED

    async def __rollback(self):
        query = self._make_rollback_query()
        try:
            await self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.ROLLEDBACK

    async def commit(self) -> None:
        """Exit the transaction or savepoint block and commit changes."""
        if self._managed:
            raise errors.InterfaceError(
                'cannot manually commit from within an `async with` block')
        await self.__commit()

    async def rollback(self) -> None:
        """Exit the transaction or savepoint block and rollback changes."""
        if self._managed:
            raise errors.InterfaceError(
                'cannot manually rollback from within an `async with` block')
        await self.__rollback()


class Transaction(BaseTransaction):

    def __enter__(self):
        if self._managed:
            raise errors.InterfaceError(
                'cannot enter context: already in a `with` block')
        self._managed = True
        self.start()

    def __exit__(self, extype, ex, tb):
        try:
            if extype is not None:
                self.__rollback()
            else:
                self.__commit()
        finally:
            self._managed = False

    def start(self) -> None:
        """Enter the transaction or savepoint block."""
        self._connection.ensure_connected()
        self._connection_inner = self._connection._inner
        self._connection_impl = self._connection_inner._impl
        query = self._make_start_query()
        try:
            self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.STARTED

    def __commit(self):
        query = self._make_commit_query()
        try:
            self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.COMMITTED

    def __rollback(self):
        query = self._make_rollback_query()
        try:
            self._connection_impl.privileged_execute(query)
        except BaseException:
            self._state = TransactionState.FAILED
            raise
        else:
            self._state = TransactionState.ROLLEDBACK

    def commit(self) -> None:
        """Exit the transaction or savepoint block and commit changes."""
        if self._managed:
            raise errors.InterfaceError(
                'cannot manually commit from within a `with` block')
        self.__commit()

    def rollback(self) -> None:
        """Exit the transaction or savepoint block and rollback changes."""
        if self._managed:
            raise errors.InterfaceError(
                'cannot manually rollback from within a `with` block')
        self.__rollback()
