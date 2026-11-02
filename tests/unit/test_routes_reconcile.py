from unittest.mock import patch
import pytest

CLAIMS = {'email': 'scheduler@test-project.iam.gserviceaccount.com', 'email_verified': True}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('RECONCILE_INVOKER', CLAIMS['email'])
    monkeypatch.setenv('RECONCILE_AUDIENCE', 'reconcile')


@pytest.fixture
def reconcile_service():
    with patch('app.routes.reconcile.ReconcileService') as service_class:
        service_class.return_value.run.return_value = {'redelivered': 1, 'deleted': 0, 'created': 2}
        yield service_class.return_value


def post(client, token='good'):
    return client.post('/reconcile', headers={'Authorization': f'Bearer {token}'})


def test_reconcile_is_off_unless_configured(client, reconcile_service):
    assert post(client).status_code == 404
    reconcile_service.run.assert_not_called()


@patch('app.routes.reconcile.id_token.verify_oauth2_token', return_value=CLAIMS)
def test_scheduler_call_runs_a_reconcile_pass(mock_verify, client, configured, reconcile_service):
    response = post(client)
    assert response.status_code == 200
    assert response.json == {'status': 'success', 'redelivered': 1, 'deleted': 0, 'created': 2}
    assert mock_verify.call_args.kwargs['audience'] == 'reconcile'


@pytest.mark.parametrize('claims', [
    {**CLAIMS, 'email': 'someone-else@example.com'},
    {**CLAIMS, 'email_verified': False},
])
def test_tokens_from_other_identities_are_rejected(client, configured, reconcile_service, claims):
    with patch('app.routes.reconcile.id_token.verify_oauth2_token', return_value=claims):
        assert post(client).status_code == 403
    reconcile_service.run.assert_not_called()


@patch('app.routes.reconcile.id_token.verify_oauth2_token', side_effect=ValueError('bad signature'))
def test_invalid_or_missing_tokens_are_rejected(mock_verify, client, configured, reconcile_service):
    assert post(client).status_code == 403
    assert client.post('/reconcile').status_code == 403
    reconcile_service.run.assert_not_called()
