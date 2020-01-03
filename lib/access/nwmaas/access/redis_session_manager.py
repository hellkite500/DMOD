import os
from typing import Optional

from redis import Redis
from redis.client import Pipeline

from nwmaas.communication import FullAuthSession, SessionManager


class RedisBackendSessionManager(SessionManager):
    _SESSION_KEY_PREFIX = 'session:'
    _SESSION_HASH_SUBKEY_SECRET = 'secret'
    _SESSION_HASH_SUBKEY_CREATED = 'created'
    #_USER_KEY_PREFIX = 'user:'
    #_USER_HASH_SUBKEY_ACCESS_TYPES = 'access_types'

    @classmethod
    def get_key_for_session(cls, session: FullAuthSession):
        return cls.get_key_for_session_by_id(session.session_id)

    @classmethod
    def get_key_for_session_by_id(cls, session_id):
        return cls.get_session_key_prefix() + str(session_id)

    @classmethod
    def get_session_key_prefix(cls):
        return cls._SESSION_KEY_PREFIX

    #@classmethod
    #def get_user_key_prefix(cls):
    #    return cls._USER_KEY_PREFIX

    def __init__(self):
        self._redis_host = os.environ.get("REDIS_HOST", "redis")
        self._redis_port = os.environ.get("REDIS_PORT", 6379)
        self._redis_pass = os.environ.get("REDIS_PASS", '')
        #print('****************** redis host is: ' + self._redis_host)

        self._redis = None

        self._next_session_id_key = 'next_session_id'
        self._all_session_secrets_hash_key = 'all_session_secrets'

        self._session_redis_hash_subkey_ip_address = 'ip_address'
        self._session_redis_hash_subkey_secret = 'secret'
        self._session_redis_hash_subkey_user = 'user'
        self._session_redis_hash_subkey_created = 'created'

    def _update_session_record(self, session: FullAuthSession, pipeline: Pipeline, do_ip_address=False, do_secret=False,
                               do_user=False):
        """
        Append to the execution tasks (without triggering execution) of a provided Pipeline to update appropriate
        properties of a serialized Session hash record in Redis.

        Parameters
        ----------
        session: DetailedSession
            The deserialized, updated Session object from which some data in a Redis session hash data structure should
            be updated.
        pipeline: Pipeline
            The created Redis transactional pipeline.
        do_ip_address: bool
            Whether the ip_address key value should be updated for the session record.
        do_secret: bool
            Whether the secret key value should be updated for the session record.
        do_user: bool
            Whether the user key value should be updated for the session record.

        Returns
        -------

        """
        session_key = self.get_key_for_session(session)
        # Build a map of the valid hash structure sub-keys in redis to tuples of (should-update-field-flag, new-value)
        keys_and_flags = {
            'ip_address': (do_ip_address, session.ip_address),
            'secret': (do_secret, session.session_secret),
            'user': (do_user, session.user)
        }
        for key in keys_and_flags:
            if keys_and_flags[key][0]:
                pipeline.hset(session_key, key, keys_and_flags[key][1])

    def create_session(self, ip_address, username) -> FullAuthSession:
        pipeline = self.redis.pipeline()
        try:
            pipeline.watch(self._next_session_id_key)
            # Remember, Redis only persists strings (though it can implicitly convert from int to string on its side)
            session_id: Optional[str] = pipeline.get(self._next_session_id_key)
            if session_id is None:
                session_id = 1
                pipeline.set(self._next_session_id_key, 2)
            else:
                pipeline.incr(self._next_session_id_key, 1)
            session = FullAuthSession(ip_address=ip_address, session_id=int(session_id), user=username)
            session_key = self.get_key_for_session(session)
            pipeline.hset(session_key, self._session_redis_hash_subkey_ip_address, session.ip_address)
            pipeline.hset(session_key, self._session_redis_hash_subkey_secret, session.session_secret)
            pipeline.hset(session_key, self._session_redis_hash_subkey_user, session.user)
            pipeline.hset(session_key, self._session_redis_hash_subkey_created, session.get_created_serialized())
            pipeline.hset(self._all_session_secrets_hash_key, session.session_secret, session.session_id)
            pipeline.execute()
            return session
        finally:
            pipeline.unwatch()
            pipeline.reset()

    def lookup_session(self, secret) -> FullAuthSession:
        session_id: Optional[str] = self.redis.hget(self._all_session_secrets_hash_key, secret)
        if session_id is not None:
            record_hash = self.redis.hgetall(self.get_key_for_session_by_id(session_id))
            session = FullAuthSession(session_id=int(session_id),
                                      session_secret=record_hash[self._session_redis_hash_subkey_secret],
                                      #created=record_hash[self._session_redis_hash_subkey_created])
                                      created=record_hash[self._session_redis_hash_subkey_created],
                                      ip_address=record_hash[self._session_redis_hash_subkey_ip_address],
                                      user=record_hash[self._session_redis_hash_subkey_user])
            return session
        else:
            return None

    @property
    def redis(self):
        if self._redis is None:
            self._redis = Redis(host=self._redis_host,
                                port=self._redis_port,
                                # db=0, encoding="utf-8", decode_responses=True,
                                db=0,
                                decode_responses=True,
                                password=self._redis_pass)
        return self._redis

    def remove_session(self, session: FullAuthSession):
        pipeline = self.redis.pipeline()
        pipeline.delete(self.get_key_for_session(session))
        pipeline.hdel(self._all_session_secrets_hash_key, session.session_secret)
        pipeline.execute()