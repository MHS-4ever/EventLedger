import os

# These values exist only in the pytest process and never access a real database.
os.environ["POSTGRES_DB"] = "eventledger_test"
os.environ["POSTGRES_USER"] = "eventledger_test"
os.environ["POSTGRES_PASSWORD"] = "test-database-password-not-a-secret"
os.environ["EVENTLEDGER_API_KEY"] = "test-api-key-not-a-secret"
os.environ["EVENTLEDGER_SOURCE_ID"] = "test-application"

