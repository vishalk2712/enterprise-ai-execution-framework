"""Small bounded RESP2 client for a dedicated Redis Streams delivery queue."""
import re
import socket
import ssl
from urllib.parse import urlsplit, unquote


class RedisBroker:
    def __init__(self, url, namespace):
        parsed = urlsplit(url)
        if parsed.scheme not in {'redis', 'rediss'} or not parsed.hostname or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/0'):
            raise ValueError('Use a redis/rediss URL for database 0')
        if not re.fullmatch(r'[a-f0-9]{32}', namespace):
            raise ValueError('Invalid coordinator namespace')
        if parsed.scheme == 'redis' and parsed.hostname not in {'127.0.0.1', 'localhost'}:
            raise ValueError('Remote Redis requires TLS (rediss)')
        self.url = parsed
        self.stream = 'outcome:' + namespace
        self.group = 'execution-v1'
        self.dlq = 'outcome:dlq:'+namespace
        self.dlq_index = 'outcome:dlq-index:'+namespace
        self.cursor = '0-0'

    def command(self, *parts):
        with socket.create_connection((self.url.hostname, self.url.port or 6379), timeout=4) as plain:
            connection = ssl.create_default_context().wrap_socket(plain, server_hostname=self.url.hostname) if self.url.scheme == 'rediss' else plain
            with connection, connection.makefile('rb') as reader:
                def send(items):
                    values = [str(v).encode() for v in items]
                    connection.sendall(b'*'+str(len(values)).encode()+b'\r\n'+b''.join(b'$'+str(len(v)).encode()+b'\r\n'+v+b'\r\n' for v in values))
                def receive(depth=0):
                    if depth > 8:
                        raise ValueError('Redis nesting exceeded bound')
                    line = reader.readline(4097)
                    if not line.endswith(b'\r\n') or len(line) > 4096:
                        raise ValueError('Redis response invalid')
                    kind, value = line[:1], line[1:-2]
                    if kind == b'+': return value.decode()
                    if kind == b'-': raise ValueError('Redis command rejected: ' + ('group exists' if value.startswith(b'BUSYGROUP') else 'check broker availability and configuration'))
                    if kind == b':': return int(value)
                    if kind == b'$':
                        length = int(value)
                        if length == -1: return None
                        if not 0 <= length <= 65536: raise ValueError('Redis value exceeded bound')
                        data = reader.read(length+2)
                        if len(data) != length+2 or not data.endswith(b'\r\n'): raise ValueError('Incomplete Redis value')
                        return data[:-2].decode()
                    if kind == b'*':
                        length = int(value)
                        if length == -1: return None
                        if not 0 <= length <= 100: raise ValueError('Redis array exceeded bound')
                        return [receive(depth+1) for _ in range(length)]
                    raise ValueError('Unsupported Redis response')
                if self.url.password:
                    auth = ['AUTH'] + ([unquote(self.url.username)] if self.url.username else []) + [unquote(self.url.password)]
                    send(auth)
                    receive()
                send(parts)
                return receive()

    def initialize(self):
        if self.command('PING') != 'PONG':
            raise ValueError('Redis is unavailable')
        try:
            self.command('XGROUP', 'CREATE', self.stream, self.group, '0', 'MKSTREAM')
        except ValueError as exc:
            if 'group exists' not in str(exc): raise

    def publish(self, action_id, digest, epoch):
        return self.command('XADD', self.stream, '*', 'action_id', action_id, 'intent_hash', digest, 'epoch', epoch)

    def pop(self, consumer, idle_ms=95000):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', consumer):
            raise ValueError('Invalid consumer identity')
        recovered = self.command('XAUTOCLAIM', self.stream, self.group, consumer, idle_ms, self.cursor, 'COUNT', 1)
        self.cursor = recovered[0]
        entries = recovered[1]
        if not entries:
            result = self.command('XREADGROUP', 'GROUP', self.group, consumer, 'COUNT', 1, 'BLOCK', 1000, 'STREAMS', self.stream, '>')
            entries = result[0][1] if result else []
        if not entries: return None
        message_id, fields = entries[0]
        return message_id, dict(zip(fields[::2], fields[1::2]))

    def ack(self, message_id):
        # Dedicated stream with one configured consumer group. Never trim pending jobs.
        return self.command('EVAL', "local n=redis.call('XACK',KEYS[1],ARGV[1],ARGV[2]); if n==1 then redis.call('XDEL',KEYS[1],ARGV[2]); end; return n", 1, self.stream, self.group, message_id)

    def dead_letter(self, reference, message_id=''):
        if set(reference)!={'action_id','intent_hash','epoch','attempts','reason_code'}: raise ValueError('Invalid dead-letter reference')
        script = """local key=ARGV[1]..':'..ARGV[3]; local id=redis.call('HGET',KEYS[3],key);
        if not id then id=redis.call('XADD',KEYS[2],'*','action_id',ARGV[1],'intent_hash',ARGV[2],'epoch',ARGV[3],'attempts',ARGV[4],'reason_code',ARGV[5]); redis.call('HSET',KEYS[3],key,id); end;
        if ARGV[7]~='' then redis.call('XACK',KEYS[1],ARGV[6],ARGV[7]); redis.call('XDEL',KEYS[1],ARGV[7]); end; return id"""
        return self.command('EVAL',script,3,self.stream,self.dlq,self.dlq_index,
                            *(reference[k] for k in ('action_id','intent_hash','epoch','attempts','reason_code')),self.group,message_id)
