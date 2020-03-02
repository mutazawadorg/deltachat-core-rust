from __future__ import print_function
import os
import sys
import pytest
import requests
from contextlib import contextmanager
import time
from . import Account, const
from .tracker import ConfigureTracker
from .capi import lib
from .hookspec import account_hookimpl
from .eventlogger import FFIEventLogger, FFIEventTracker
from _pytest.monkeypatch import MonkeyPatch
import tempfile


def pytest_addoption(parser):
    parser.addoption(
        "--liveconfig", action="store", default=None,
        help="a file with >=2 lines where each line "
             "contains NAME=VALUE config settings for one account"
    )
    parser.addoption(
        "--ignored", action="store_true",
        help="Also run tests marked with the ignored marker",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "ignored: Mark test as bing slow, skipped unless --ignored is used."
    )
    cfg = config.getoption('--liveconfig')
    if not cfg:
        cfg = os.getenv('DCC_NEW_TMP_EMAIL')
        if cfg:
            config.option.liveconfig = cfg


def pytest_runtest_setup(item):
    if (list(item.iter_markers(name="ignored"))
            and not item.config.getoption("ignored")):
        pytest.skip("Ignored tests not requested, use --ignored")


def pytest_report_header(config, startdir):
    summary = []

    t = tempfile.mktemp()
    m = MonkeyPatch()
    try:
        m.setattr(sys.stdout, "write", lambda x: len(x))
        ac = Account(t)
        info = ac.get_info()
        ac.shutdown()
    finally:
        m.undo()
        os.remove(t)
    summary.extend(['Deltachat core={} sqlite={}'.format(
         info['deltachat_core_version'],
         info['sqlite_version'],
     )])

    cfg = config.option.liveconfig
    if cfg:
        if "#" in cfg:
            url, token = cfg.split("#", 1)
            summary.append('Liveconfig provider: {}#<token ommitted>'.format(url))
        else:
            summary.append('Liveconfig file: {}'.format(cfg))
    return summary


class SessionLiveConfigFromFile:
    def __init__(self, fn):
        self.fn = fn
        self.configlist = []
        for line in open(fn):
            if line.strip() and not line.strip().startswith('#'):
                d = {}
                for part in line.split():
                    name, value = part.split("=")
                    d[name] = value
                self.configlist.append(d)

    def get(self, index):
        return self.configlist[index]

    def exists(self):
        return bool(self.configlist)


class SessionLiveConfigFromURL:
    def __init__(self, url):
        self.configlist = []
        self.url = url

    def get(self, index):
        try:
            return self.configlist[index]
        except IndexError:
            assert index == len(self.configlist), index
            res = requests.post(self.url)
            if res.status_code != 200:
                pytest.skip("creating newtmpuser failed {!r}".format(res))
            d = res.json()
            config = dict(addr=d["email"], mail_pw=d["password"])
            self.configlist.append(config)
            return config

    def exists(self):
        return bool(self.configlist)


@pytest.fixture(scope="session")
def session_liveconfig(request):
    liveconfig_opt = request.config.option.liveconfig
    if liveconfig_opt:
        if liveconfig_opt.startswith("http"):
            return SessionLiveConfigFromURL(liveconfig_opt)
        else:
            return SessionLiveConfigFromFile(liveconfig_opt)


@pytest.fixture
def acfactory(pytestconfig, tmpdir, request, session_liveconfig, datadir):

    class AccountMaker:
        def __init__(self):
            self.live_count = 0
            self.offline_count = 0
            self._finalizers = []
            self.init_time = time.time()
            self._generated_keys = ["alice", "bob", "charlie",
                                    "dom", "elena", "fiona"]

        def finalize(self):
            while self._finalizers:
                fin = self._finalizers.pop()
                fin()

        def make_account(self, path, logid):
            ac = Account(path)
            ac._evtracker = ac.add_account_plugin(FFIEventTracker(ac))
            ac._configtracker = ac.add_account_plugin(ConfigureTracker())
            ac.add_account_plugin(FFIEventLogger(ac, logid=logid))
            self._finalizers.append(ac.shutdown)
            return ac

        def get_unconfigured_account(self):
            self.offline_count += 1
            tmpdb = tmpdir.join("offlinedb%d" % self.offline_count)
            ac = self.make_account(tmpdb.strpath, logid="ac{}".format(self.offline_count))
            ac._evtracker.init_time = self.init_time
            ac._evtracker.set_timeout(2)
            return ac

        def _preconfigure_key(self, account, addr):
            # Only set a key if we haven't used it yet for another account.
            if self._generated_keys:
                keyname = self._generated_keys.pop(0)
                fname_pub = "key/{name}-public.asc".format(name=keyname)
                fname_sec = "key/{name}-secret.asc".format(name=keyname)
                account._preconfigure_keypair(addr,
                                              datadir.join(fname_pub).read(),
                                              datadir.join(fname_sec).read())

        def get_configured_offline_account(self):
            ac = self.get_unconfigured_account()

            # do a pseudo-configured account
            addr = "addr{}@offline.org".format(self.offline_count)
            ac.set_config("addr", addr)
            self._preconfigure_key(ac, addr)
            lib.dc_set_config(ac._dc_context, b"configured_addr", addr.encode("ascii"))
            ac.set_config("mail_pw", "123")
            lib.dc_set_config(ac._dc_context, b"configured_mail_pw", b"123")
            lib.dc_set_config(ac._dc_context, b"configured", b"1")
            return ac

        def get_online_config(self, pre_generated_key=True):
            if not session_liveconfig:
                pytest.skip("specify DCC_NEW_TMP_EMAIL or --liveconfig")
            configdict = session_liveconfig.get(self.live_count)
            self.live_count += 1
            if "e2ee_enabled" not in configdict:
                configdict["e2ee_enabled"] = "1"

            # Enable strict certificate checks for online accounts
            configdict["imap_certificate_checks"] = str(const.DC_CERTCK_STRICT)
            configdict["smtp_certificate_checks"] = str(const.DC_CERTCK_STRICT)

            tmpdb = tmpdir.join("livedb%d" % self.live_count)
            ac = self.make_account(tmpdb.strpath, logid="ac{}".format(self.live_count))
            if pre_generated_key:
                self._preconfigure_key(ac, configdict['addr'])
            ac._evtracker.init_time = self.init_time
            ac._evtracker.set_timeout(30)
            return ac, dict(configdict)

        def get_online_configuring_account(self, mvbox=False, sentbox=False, move=False,
                                           pre_generated_key=True, config={}):
            ac, configdict = self.get_online_config(
                pre_generated_key=pre_generated_key)
            configdict.update(config)
            configdict["mvbox_watch"] = str(int(mvbox))
            configdict["mvbox_move"] = str(int(move))
            configdict["sentbox_watch"] = str(int(sentbox))
            ac.update_config(configdict)
            ac.start()
            return ac

        def get_one_online_account(self, pre_generated_key=True, mvbox=False, move=False):
            ac1 = self.get_online_configuring_account(
                pre_generated_key=pre_generated_key, mvbox=mvbox, move=move)
            ac1._configtracker.wait_imap_connected()
            ac1._configtracker.wait_smtp_connected()
            ac1._configtracker.wait_finish()
            return ac1

        def get_two_online_accounts(self, move=False):
            ac1 = self.get_online_configuring_account(move=True)
            ac2 = self.get_online_configuring_account()
            ac1._configtracker.wait_finish()
            ac2._configtracker.wait_finish()
            return ac1, ac2

        def clone_online_account(self, account, pre_generated_key=True):
            self.live_count += 1
            tmpdb = tmpdir.join("livedb%d" % self.live_count)
            ac = self.make_account(tmpdb.strpath, logid="ac{}".format(self.live_count))
            if pre_generated_key:
                self._preconfigure_key(ac, account.get_config("addr"))
            ac._evtracker.init_time = self.init_time
            ac._evtracker.set_timeout(30)
            ac.update_config(dict(
                addr=account.get_config("addr"),
                mail_pw=account.get_config("mail_pw"),
                mvbox_watch=account.get_config("mvbox_watch"),
                mvbox_move=account.get_config("mvbox_move"),
                sentbox_watch=account.get_config("sentbox_watch"),
            ))
            ac.start()
            return ac

    am = AccountMaker()
    request.addfinalizer(am.finalize)
    return am


@pytest.fixture
def tmp_db_path(tmpdir):
    return tmpdir.join("test.db").strpath


@pytest.fixture
def lp():
    class Printer:
        def sec(self, msg):
            print()
            print("=" * 10, msg, "=" * 10)

        def step(self, msg):
            print("-" * 5, "step " + msg, "-" * 5)
    return Printer()


@pytest.fixture
def make_plugin_recorder():
    @contextmanager
    def make_plugin_recorder(account):
        class HookImpl:
            def __init__(self):
                self.calls_member_added = []

            @account_hookimpl
            def member_added(self, chat, contact):
                self.calls_member_added.append(dict(chat=chat, contact=contact))

            def get_first(self, name):
                val = getattr(self, "calls_" + name, None)
                if val is not None:
                    return val.pop(0)

        with account.temp_plugin(HookImpl()) as plugin:
            yield plugin

    return make_plugin_recorder