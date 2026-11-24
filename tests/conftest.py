import fakeredis
import pgserver
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app.auth import AuthService, seed_users
from app.cache import Cache
from app.db import Database
from app.policy_rag import PolicyStore


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"))
    yield server
    server.cleanup()


@pytest.fixture(scope="session")
def db(pg):
    database = Database(pg.get_uri())
    database.init_schema()
    yield database
    database.close()


@pytest.fixture(scope="session")
def saver(db):
    s = PostgresSaver(db.pool)
    s.setup()
    return s


@pytest.fixture(scope="session")
def policies(db):
    store = PolicyStore(db, Cache(fakeredis.FakeRedis(decode_responses=True)))
    store.ingest()
    return store


@pytest.fixture
def cache():
    return Cache(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture(scope="session")
def users(db):
    seed_users(db, {"alice": ("pw-alice", "manager"), "bob": ("pw-bob", "manager"),
                    "vic": ("pw-vic", "viewer"), "root": ("pw-root", "admin")})
