import re
import sys
import threading
import websocket
#import socket
import ssl
import json
import time
import urllib
import queue
from itertools import cycle
from grapheneapi.websocket import Websocket
from bitsharesbase.chains import known_chains
from bitsharesapi import exceptions
import logging
log = logging.getLogger(__name__)

class TimedOut(exceptions.NumRetriesReached):
    pass

class BitSharesNodeRPC(object):

    def __init__(self, urls, user="", password="", **kwargs):
        if isinstance(urls, list):
            self.urls = cycle(urls)
        else:
            self.urls = cycle([urls])
        self.user = user
        self.password = password

        self._rh = kwargs.pop("request_handler", None)

        self.connection_id = 0
        self.last_reply = 0

        self.rate_limit = kwargs.pop("rate_limit", 0.005)

        self.prepare_proxy(kwargs)
        self.notes = queue.Queue()
        self.requests = queue.Queue()
        self.replies = {}#queue.Queue()
        self.replylock = threading.Lock()
        self.errors = queue.Queue()
        self.ws = None # hack
        self.needed = True
        self.initialized = False
        self.keep_connecting = False
        self.connecting = False
        self.connected = False
        self.handshake = False

        self.api_id = {}
        self._request_id = 0
        self._subscription_id = 100
        if isinstance(urls, list):
            self.urls = cycle(urls)
        else:
            self.urls = cycle([urls])
        self.user = user
        self.password = password
        self.num_retries = kwargs.get("num_retries", -1)

        #self.wsconnect()
        #self.register_apis()

        self.thread = threading.Thread(target=self.__forever, args=( ))
        self.thread.start()

        #super(BitSharesNodeRPC, self).__init__(*args, **kwargs)
        #self.chain_params = self.get_network()

    def register_apis(self):
        self.api_id["database"] = self.database(api_id=1)
        self.api_id["history"] = self.history(api_id=1)
        self.api_id["network_broadcast"] = self.network_broadcast(api_id=1)


    def get_request_id(self):
        self._request_id += 1
        return self._request_id

    def get_subscription_id(self):
        self._subscription_id += 1
        return self._subscription_id

    def report(self, note, error=None):
        if not self._rh:
            return
        self._rh.ping(0, (self, note, error))

    def disconnect(self):
        self.close()

    def close(self):
        self.needed = False
        self.connecting = False
        self.connected = False

    def __forever(self):
        done_ev = "connected"
        doin_ev = "connecting"
        fail_ev = "failed"
        while self.needed:
            if not(self.connected):
                self.initialized = False
                if self.ws:
                    try:
                        self.ws.close()
                    except:
                        pass
                    self.ws = None
                self.report(doin_ev)
                time.sleep(0.1)
                try:
                    self.wsconnect()
                    #tm = self.ws.sock.gettimeout()
                    if not self.needed:
                        break
                    self.handshake = True
#                    tm = self.ws.sock.gettimeout()
                    log.debug("now login")
                    self.login(self.user, self.password, api_id=1, plan_b=True)
                    log.debug("now reg api")
                    self.register_apis(plan_b=True)
                    log.debug("now chain params")
                    self.chain_params = self.get_network(plan_b=True)
                    self.market_buckets = self.get_market_history_buckets(plan_b=True, api="history")
                    self.initialized = True

                except Exception as error:
                    #log.error(str(error))#, type(error))
                    self.handshake = False
                    if not(self.keep_connecting):
                        self.report("failed", error)
                        break
                    self.report("attempt_failed", error)
                    continue
                log.debug("now done")
                self.connected = True
                self.report(done_ev)
                done_ev = "reconnected"
                doin_ev = "reconnecting"
                continue

            self.keep_connecting = self.needed

            try:
                payload = self.requests.get(block=False)
                self.ws.sock.settimeout(None)
                self.ws.send(json.dumps(payload, ensure_ascii=False).encode('utf8'))
                payload['_connection_id'] = self.connection_id
            except KeyboardInterrupt:
                raise
            except queue.Empty:
                pass
            except Exception as error:
                #import traceback
                #traceback.print_exc()
                self.connected = False
                self.report("disconnected", error)
                continue

            if not(self.needed):
                break

            try:
                self.ws.sock.settimeout(self.rate_limit) # something not too high
                reply = self.ws.recv()
            except KeyboardInterrupt:
                raise
            except websocket._exceptions.WebSocketTimeoutException:
                try:
                    self.ws.sock.settimeout(None)
                    self.ws.ping()
                    continue
                except Exception as error:
                    self.connected = False
                    self.report("disconnected", error)
                    continue
            # yes ^ v - same code
            except Exception as error:
                self.connected = False
                self.report("disconnected", error)
                continue

            #log.debug("REPLY: %s", reply)
            ret = {}
            err = None
            try:
                ret = json.loads(reply, strict=False)
            except ValueError:
                err = ValueError("Client returned invalid format. Expected JSON!")
            except Exception as e:
                err = e
            except BaseException as e:
                err = e
            if err:
                self.errors.put(err)
                continue

            if not('id' in ret) or ('method' in ret and ret['method'] == 'notice'):
                self.notes.put( ret['params'] )
                continue

            with self.replylock:
                self.replies[ ret['id'] ] = ret

        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None

        self.needed = False
        self.connected = False
        self.report("done")

    def flush_notes(self):
        notes = [ ]
        while True:
            try:
                notice = self.notes.get(block=False)
            except:
                break
            notes.append( notice )
        return notes

    def register_apis(self, plan_b=False):
        self.api_id["database"] = self.database(api_id=1, plan_b=plan_b)
        self.api_id["history"] = self.history(api_id=1, plan_b=plan_b)
        self.api_id["network_broadcast"] = self.network_broadcast(api_id=1, plan_b=plan_b)

    def prepare_proxy(self, options):
        proxy_url = options.pop("proxy", None)
        if proxy_url:
            try:
                url = urllib.parse.urlparse(proxy_url)
                self.proxy_host = url.hostname
                self.proxy_port = url.port
                self.proxy_type = url.scheme.lower()
                self.proxy_user = url.username
                self.proxy_pass = url.password
            except Exception as e:
                raise ValueError("Can not parse proxy URL %s -- %s" % (proxy_url, str(e)))
        else:
            # Defaults (tweakable)
            self.proxy_host = options.pop("proxy_host", None)
            self.proxy_port = options.pop("proxy_port", 80)
            self.proxy_type = options.pop("proxy_type", 'http')
            self.proxy_user = options.pop("proxy_user", None)
            self.proxy_pass = options.pop("proxy_pass", None)
        self.proxy_rdns = True
        if not(self.proxy_type.endswith('h')):
            self.proxy_rdns = False
        #else:
        #    self.proxy_type = self.proxy_type[0:len(self.proxy_type)-1]
        if self.proxy_port is None:
            self.proxy_port = 80

        log.info("Using proxy %s:%d %s" % (self.proxy_host, self.proxy_port, self.proxy_type))

    def rpcexec(self, payload, planb=False):
        """ Execute a call by sending the payload.
            It makes use of the GrapheneRPC library.
            In here, we mostly deal with BitShares specific error handling

            :param json payload: Payload data
            :raises ValueError: if the server does not respond in proper JSON format
            :raises RPCError: if the server returns an error
        """
        try:
            # Forward call to GrapheneWebsocketRPC and catch+evaluate errors
            #return super(BitSharesNodeRPC, self).rpcexec(payload)
            if planb:
                return self._rpcexec_b(payload)
            return self._rpcexec(payload)
        except exceptions.RPCError as e:
            msg = exceptions.decodeRPCErrorMsg(e).strip()
            if msg == "missing required active authority":
                raise exceptions.MissingRequiredActiveAuthority
            elif re.match("^no method with name.*", msg):
                raise exceptions.NoMethodWithName(msg)
            elif msg:
                raise exceptions.UnhandledRPCError(msg)
            else:
                raise e
        except Exception as e:
            raise e

    def _rpcexec_b(self, payload):
        """ Execute a call by sending the payload

            :param json payload: Payload data
            :raises ValueError: if the server does not respond in proper JSON format
            :raises RPCError: if the server returns an error
        """
        #log.debug(json.dumps(payload))
        log.debug("RPC-exec blocking %d for %s %s", payload['id'], payload['params'][1], payload['params'][2])

        if not self.needed:
            raise TimedOut()

        self.ws.send(json.dumps(payload, ensure_ascii=False).encode('utf8'))
        reply = self.ws.recv()

        ret = {}
        try:
            ret = json.loads(reply, strict=False)
        except ValueError:
            raise ValueError("Client returned invalid format. Expected JSON!")

        #log.debug(json.dumps(reply))

        self.last_reply += 1

        if 'error' in ret:
            if 'detail' in ret['error']:
                raise exceptions.RPCError(ret['error']['detail'])
            else:
                raise exceptions.RPCError(ret['error']['message'])
        else:
            return ret["result"]


    """ RPC Calls
    """
    def _rpcexec(self, payload):
        """ Execute a call by sending the payload

            :param json payload: Payload data
            :raises ValueError: if the server does not respond in proper JSON format
            :raises RPCError: if the server returns an error
        """
        if not(self.needed):
            raise exceptions.NumRetriesReached()
        #log.debug(json.dumps(payload))
        call_id = payload['id']
        log.debug("RPC-exec request %d %s %s", call_id, payload['params'][1], payload['params'][2])
        #if payload['params'][1] == "lookup_account_names":
        #    raise Exception("NO")
        try:
            #if self.connected:
            #    self.wssend(payload)
            #else:
            self.requests.put(payload, block=True)
        except KeyboardInterrupt:
            raise
        except:
            raise Exception("Unable to queue request")

        sleepbase = 120 * 3 if self.proxy_type else 1
        sleeptime = sleepbase

        preid = int(self.connection_id)
        seen_reply = int(self.last_reply)
        error = None
        attempts = 0
        while True:
            if self.initialized and not(self.connected) and not(self.connecting):
                raise TimedOut() #exceptions.NumRetriesReached
            #print("RPC-exec waiting for", call_id, payload['params'][1], payload['params'][2], "[", "%.03f" % sleeptime, "]","/",attempts)
            try:
                error = self.errors.get(block=False)
            except KeyboardInterrupt:
                raise
            except:
                pass

            if "_connection_id" in payload: # request sent on differen conn?
                if self.connection_id > payload["_connection_id"]:
                    if error:               # reply will never come, then
                        raise error
                    raise TimedOut()

            attempts = self.connection_id - preid
            if ((self.num_retries >= 0 and attempts >= self.num_retries)
                and not(self.connected)):
                self.connecting = False
                if error:
                    raise error
                raise exceptions.NumRetriesReached()

            if (sleeptime <= 0) or not(self.needed):
                self.connecting = False
                if error:
                    raise error
                raise TimedOut()

            with self.replylock:
                ret = self.replies.pop(call_id, None)
            #sleeptime = (cnt - 1) * 2 if cnt < 10 else 10

            if not ret:
                # other requests are fine
                new_reply = int(self.last_reply)
                if new_reply != seen_reply:
                    seen_reply = new_reply
                    # let's sleep some more, then!
                    sleeptime = sleepbase
                    continue
                sleeptime -= self.rate_limit*2
                time.sleep(self.rate_limit*2)
                continue

            self.last_reply += 1

            if 'error' in ret:
                from pprint import pprint
                pprint(ret)
                if 'detail' in ret['error']:
                    raise exceptions.RPCError(ret['error']['detail'])
                else:
                    raise exceptions.RPCError(ret['error']['message'])
            else:
                return ret["result"]

    #def close(self):
    #   self.shutting_down = True

    def wssend(self, payload):
        self.ws.send(json.dumps(payload, ensure_ascii=False).encode('utf8'))

    def wsconnect(self):
        self.connecting = True
        self.connected = False

        timeout = 30 # 30 seconds for regular connections
        timeout *= 2 if self.proxy_type else 1 # 1 min for proxy/tor

        cnt = 0
        while self.connecting:
            cnt += 1
            self.url = next(self.urls)
            log.debug("Trying to connect to node %s", self.url)
            sslopt_ca_certs = None

            if self.url.startswith("wss://"):
                sslopt_ca_certs = {'cert_reqs': ssl.CERT_NONE}

            self.ws = websocket.WebSocket(sslopt=sslopt_ca_certs)

            try:
                self.connection_id += 1
                self.ws.connect(self.url,
                    timeout = timeout,
                    http_proxy_host = self.proxy_host,
                    http_proxy_port = self.proxy_port,
                    proxy_type = self.proxy_type,
                    http_proxy_auth = (
                        (self.proxy_user, self.proxy_pass)
                        if (self.proxy_user or self.proxy_pass) else None
                    )
                )
                break
            except KeyboardInterrupt:
                self.connecting = False
                raise
            except Exception as error:
                if (self.num_retries >= 0 and cnt > self.num_retries):
                    self.connecting = False
                    raise error #exceptions.NumRetriesReached()
                else:
                    self.errors.put( error )

                if not self.needed:
                    break

                sleeptime = min((cnt-1) * 2, 10)#*3
                if sleeptime or True:
                    log.warning(
                        "Could not connect to node: %s (%d/%d) \n %s "
                        % (self.url, cnt, self.num_retries, str(error)) +
                        "Retrying in %d seconds" % sleeptime
                    )
                    time.sleep(sleeptime)

        if not self.connecting:
            self.ws.close()

        #self.connected = True
        self.connecting = False

        #self._restart_thread()
        #self.login(self.user, self.password, api_id=1, plan_b=True)


    def get_account(self, name, **kwargs):
        """ Get full account details from account name or id

            :param str name: Account name or account id
        """
        if len(name.split(".")) == 3:
            return self.get_objects([name])[0]
        else:
            return self.get_account_by_name(name, **kwargs)

    def get_asset(self, name, **kwargs):
        """ Get full asset from name of id

            :param str name: Symbol name or asset id (e.g. 1.3.0)
        """
        if len(name.split(".")) == 3:
            return self.get_objects([name], **kwargs)[0]
        else:
            return self.lookup_asset_symbols([name], **kwargs)[0]

    def get_object(self, o, **kwargs):
        """ Get object with id ``o``

            :param str o: Full object id
        """
        return self.get_objects([o], **kwargs)[0]

    def get_network(self, num_retries=3, plan_b=False):
        """ Identify the connected network. This call returns a
            dictionary with keys chain_id, core_symbol and prefix
        """
        props = self.get_chain_properties(num_retries=num_retries, plan_b=plan_b)
        chain_id = props["chain_id"]
        for k, v in known_chains.items():
            if v["chain_id"] == chain_id:
                return v
        raise("Connecting to unknown network!")

    def __getattr__(self, name):
        """ Map all methods to RPC calls and pass through the arguments
        """
        def method(*args, **kwargs):

            # Sepcify the api to talk to
            if "api_id" not in kwargs:
                if ("api" in kwargs):
                    if (kwargs["api"] in self.api_id and
                            self.api_id[kwargs["api"]]):
                        api_id = self.api_id[kwargs["api"]]
                    else:
                        raise ValueError(
                            "Unknown API! "
                            "Verify that you have registered to %s"
                            % kwargs["api"]
                        )
                else:
                    api_id = 0
            else:
                api_id = kwargs["api_id"]

            # let's be able to define the num_retries per query
            self.num_retries = kwargs.get("num_retries", self.num_retries)
            planb = kwargs.get("plan_b", False)

            query = {"method": "call",
                     "params": [api_id, name, list(args)],
                     "jsonrpc": "2.0",
                     "id": self.get_request_id()}
            r = self.rpcexec(query, planb=planb)
            return r
        return method
