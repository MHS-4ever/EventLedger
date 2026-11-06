"""Generate synthetic CI-only values. Never called on a deployment host."""
from pathlib import Path
import secrets


def main():
    Path('.env.test').write_text('TEST_POSTGRES_DB=eventledger_test\nTEST_POSTGRES_USER=eventledger_test\n'
                                'TEST_POSTGRES_HOST=127.0.0.1\nTEST_POSTGRES_PORT=5433\n'
                                f'TEST_POSTGRES_PASSWORD={secrets.token_hex(24)}\n', encoding='utf-8')
    path = Path('shipsafe/runtime/ci.env')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('POSTGRES_DB=shipsafe_ci\nPOSTGRES_USER=shipsafe_ci\n'
                    f'POSTGRES_PASSWORD={secrets.token_hex(24)}\nEVENTLEDGER_API_KEY={secrets.token_hex(24)}\n'
                    'EVENTLEDGER_SOURCE_ID=shipsafe-synthetic-ci\nSHIPSAFE_PROXY_PORT=8080\n', encoding='utf-8')


if __name__ == '__main__':
    main()
