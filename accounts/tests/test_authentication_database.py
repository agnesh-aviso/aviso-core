from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from aviso.framework import (AuthenticationPostgresContext, ConnectionFactory,
                             tenant_holder)
from aviso.framework.authentication import MongoBackend
from aviso.framework.postgresdb import GnanaPostgresDB


class AuthenticationBackendDatabaseTest(SimpleTestCase):

    def test_user_lookup_and_update_use_gbm_database_scope(self):
        app_user = SimpleNamespace(
            id='user-id',
            linked_to='',
            account_locked=False,
            login_attempts=1,
            failed_login_time=0,
            verify_password=mock.Mock(return_value=True),
            save=mock.Mock())

        with (
                mock.patch('aviso.domainmodel.tenant.Tenant.getByName',
                           return_value=object()),
                mock.patch('aviso.framework.authentication.User.getUserByLogin',
                           return_value=app_user),
                mock.patch('aviso.framework.authentication.sec_context.set_context'),
                mock.patch('aviso.framework.authentication.AuthenticationPostgresContext')
                as auth_database,
                mock.patch('aviso.models.GnanaUser')):
            result = MongoBackend().authenticate(
                username='tester@administrative.domain', password='secret')

        self.assertIsNotNone(result)
        self.assertEqual(auth_database.call_args_list, [
            mock.call('administrative.domain'),
            mock.call('administrative.domain'),
        ])
        app_user.save.assert_called_once_with()


class AuthenticationPostgresContextTest(SimpleTestCase):

    def setUp(self):
        self.previous_context = tenant_holder.peek_context()
        tenant_holder.set_context('tester', 'administrative.domain',
                                  'administrative.domain')

    def tearDown(self):
        tenant_holder.set_context(*self.previous_context)

    def test_authentication_scope_uses_and_closes_gbm_connection(self):
        connection = mock.Mock()
        with mock.patch.object(ConnectionFactory, 'stack_postgres_conn',
                               return_value=connection) as connect:
            with AuthenticationPostgresContext('administrative.domain'):
                self.assertIs(tenant_holder.authentication_postgres_db,
                              connection)

            connect.assert_called_once_with('administrative.domain',
                                           db_type='gbm')
            connection.close.assert_called_once_with()
            self.assertIsNone(tenant_holder.authentication_postgres_db)

    def test_only_scoped_queries_use_the_authentication_connection(self):
        auth_connection = mock.Mock()
        stack_connection = mock.Mock()
        db = GnanaPostgresDB()
        db.sec_context = SimpleNamespace(
            authentication_postgres_db=auth_connection,
            stack_postgres_db=stack_connection)

        db.tenantawaredb.cursor()
        auth_connection.cursor.assert_called_once_with()
        stack_connection.cursor.assert_not_called()

        db.sec_context.authentication_postgres_db = None
        db.tenantawaredb.cursor()
        stack_connection.cursor.assert_called_once_with()
        auth_connection.cursor.assert_called_once_with()
