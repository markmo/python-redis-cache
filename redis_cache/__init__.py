from datetime import date, datetime
from functools import wraps
from hashlib import md5
from json import JSONEncoder, dumps, loads
import logging
from time import time
from base64 import b64encode
import redis


def get_cache_lua_fn(client):
    if not hasattr(client, '_lua_cache_fn'):
        client._lua_cache_fn = client.register_script("""
local ttl = tonumber(ARGV[2])
local value
if ttl > 0 then
  value = redis.call('SETEX', KEYS[1], ttl, ARGV[1])
else
  value = redis.call('SET', KEYS[1], ARGV[1])
end
local limit = tonumber(ARGV[3])
if limit > 0 then
  local time_parts = redis.call('TIME')
  local time = tonumber(time_parts[1] .. '.' .. time_parts[2])
  redis.call('ZADD', KEYS[2], time, KEYS[1])
  local count = tonumber(redis.call('ZCOUNT', KEYS[2], '-inf', '+inf'))
  local over = count - limit
  if over > 0 then
    local stale_keys_and_scores = redis.call('ZPOPMIN', KEYS[2], over)
    -- Remove the the scores and just leave the keys
    local stale_keys = {}
    for i = 1, #stale_keys_and_scores, 2 do
      stale_keys[#stale_keys+1] = stale_keys_and_scores[i]
    end
    redis.call('ZREM', KEYS[2], unpack(stale_keys))
    redis.call('DEL', unpack(stale_keys))
  end
end
return value
""")
    return client._lua_cache_fn


class DateTimeEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()

        return JSONEncoder.default(self, obj)


def _dumps(obj):
    return dumps(obj, cls=DateTimeEncoder, default=str)


class RedisCache:

    def __init__(self, redis_client, prefix="rc", serializer=_dumps, deserializer=loads):
        self.client = redis_client
        self.prefix = prefix
        self.serializer = serializer
        self.deserializer = deserializer
        self.log = logging.getLogger('RedisCache')

    def cache(self, ttl=0, limit=0, namespace=None, ismethod=False):
        return CacheDecorator(
            redis_client=self.client,
            prefix=self.prefix,
            serializer=self.serializer,
            deserializer=self.deserializer,
            ttl=ttl,
            limit=limit,
            namespace=namespace,
            ismethod=ismethod
        )

    def mget(self, *fns_with_args):
        keys = []
        for fn_and_args in fns_with_args:
            fn = fn_and_args['fn']
            args = fn_and_args['args'] if 'args' in fn_and_args else []
            kwargs = fn_and_args['kwargs'] if 'kwargs' in fn_and_args else {}
            keys.append(fn.instance.get_key(args=args, kwargs=kwargs))

        results = self.client.mget(*keys)
        pipeline = self.client.pipeline()

        deserialized_results = []
        needs_pipeline = False
        for i, result in enumerate(results):
            if result is None:
                needs_pipeline = True

                fn_and_args = fns_with_args[i]
                fn = fn_and_args['fn']
                args = fn_and_args['args'] if 'args' in fn_and_args else []
                kwargs = fn_and_args['kwargs'] if 'kwargs' in fn_and_args else {}
                result = fn.instance.original_fn(*args, **kwargs)
                result_serialized = self.serializer(result)
                get_cache_lua_fn(self.client)(keys=[keys[i], fn.instance.keys_key], args=[result_serialized, fn.instance.ttl, fn.instance.limit], client=pipeline)
            else:
                result = self.deserializer(result)
            deserialized_results.append(result)

        if needs_pipeline: 
            pipeline.execute()
        return deserialized_results


class CacheDecorator:

    def __init__(self, redis_client, prefix="rc", serializer=dumps, deserializer=loads, ttl=0, limit=0, namespace=None, ismethod=False):
        self.client = redis_client
        self.prefix = prefix
        self.serializer = serializer
        self.deserializer = deserializer
        self.ttl = ttl
        self.limit = limit
        self.namespace = namespace
        self.keys_key = None
        self.ismethod = ismethod
        self.log = logging.getLogger('CacheDecorator')
        self.offline = False

    def get_key(self, args, kwargs):
        serialized_data = self.serializer([args, kwargs])
        if not isinstance(serialized_data, str):
            serialized_data = str(b64encode(serialized_data), 'utf-8')

        return f'{self.prefix}:{self.namespace}:{serialized_data}'

    def __call__(self, fn):
        self.namespace = self.namespace if self.namespace else f'{fn.__module__}.{fn.__name__}'
        self.keys_key = f'{self.prefix}:{self.namespace}:keys'
        self.original_fn = fn

        @wraps(fn)
        def inner(*args, **kwargs):
            nonlocal self
            if self.offline:
                return fn(*args, **kwargs)

            if self.ismethod:
                self.log.debug('is method')
                key = self.get_key(args[1:], kwargs)
            else:
                self.log.debug('is function')
                key = self.get_key(args, kwargs)

            try:
                result = self.client.get(key)
            except redis.exceptions.ConnectionError as err:
                self.log.error('Lost connection to Redis: %s', str(err), exc_info=False)
                # assume redis is out for rest of session
                self.offline = True
                return fn(*args, **kwargs)

            if not result:
                result = fn(*args, **kwargs)
                result_serialized = self.serializer(result)
                get_cache_lua_fn(self.client)(keys=[key, self.keys_key], args=[result_serialized, self.ttl, self.limit])
            else:
                result = self.deserializer(result)

            return result

        inner.invalidate = self.invalidate
        inner.invalidate_all = self.invalidate_all
        inner.instance = self
        return inner

    def invalidate(self, *args, **kwargs):
        key = self.get_key(args, kwargs)
        pipe = self.client.pipeline()
        pipe.delete(key)
        pipe.zrem(self.keys_key, key)
        pipe.execute()

    def invalidate_all(self, *args, **kwargs):
        all_keys = self.client.zrange(self.keys_key, 0, -1)
        self.client.delete(*all_keys, self.keys_key)
