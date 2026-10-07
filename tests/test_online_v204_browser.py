import asyncio
import copy
import hashlib
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest
from eventarena.browser import Browser,checkpoint_policy_hash


POLICY={'id':'test_main_v1','mode':'task_content_v1','content_selectors':['main'],
        'exclude_selectors':['nav','#onetrust-banner-sdk'],'required_text_any':['forecast']}


@pytest.mark.browser
def test_actual_har_reset_restores_factual_prefix_then_returns_to_live_web(tmp_path):
    state={'temperature':16}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=('<html><body><nav>global navigation</nav><main><h1>Forecast reference</h1>'
                  +('<p>Documented reference context and observations.</p>'*5)
                  +f"<p>Temperature {state['temperature']} C</p><input name='city' value='Vancouver'>"
                  +"<div id='onetrust-banner-sdk'>cookie choice</div></main></body></html>").encode()
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}/reference'
    async def run():
        async with Browser(tmp_path/'capture',headless=True) as browser:
            browser.checkpoint_audit_policy=POLICY
            await browser.execute({'op':'GOTO','url':url})
            original=await browser.observe()
        har=tmp_path/'capture/network.har'
        assert har.is_file()
        state['temperature']=13
        async with Browser(tmp_path/'restore',headless=True) as browser:
            browser.checkpoint_audit_policy=POLICY
            await browser.begin_checkpoint_replay({'mode':'recorded_prefix_then_live','har_path':str(har),
                                                  'sha256':hashlib.sha256(har.read_bytes()).hexdigest()})
            await browser.execute({'op':'GOTO','url':url})
            restored=await browser.observe()
            assert 'Temperature 16 C' in restored['text']
            assert restored['checkpoint_projection']['content']==original['checkpoint_projection']['content']
            assert restored['checkpoint_projection']['policy_sha256']==checkpoint_policy_hash(POLICY)
            await browser.end_checkpoint_replay()
            await browser.execute({'op':'GOTO','url':url})
            live=await browser.observe()
            assert 'Temperature 13 C' in live['text']
            assert live['network_mode']=='live_after_checkpoint'
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.browser
def test_projection_ignores_declared_ui_but_preserves_all_task_values(tmp_path):
    async def run():
        async with Browser(tmp_path,headless=True) as browser:
            browser.checkpoint_audit_policy=POLICY
            await browser.page.set_content('<nav>menu</nav><main><h1>Forecast reference</h1>'+
                '<p>weather observations and task context.</p>'*6+
                '<p id="temperature">16 C</p><input name="city" value="Vancouver">'
                '<div id="onetrust-banner-sdk">cookie</div></main>')
            before=await browser.observe()
            await browser.page.evaluate("document.querySelector('nav').innerText='changed';document.querySelector('#onetrust-banner-sdk').remove()")
            ui=await browser.observe()
            assert before['text']!=ui['text']
            assert before['checkpoint_projection']['content']==ui['checkpoint_projection']['content']
            await browser.page.evaluate("document.querySelector('#temperature').innerText='13 C';document.querySelector('input').value='Calgary'")
            changed=await browser.observe()
            assert changed['checkpoint_projection']['content']!=ui['checkpoint_projection']['content']
            assert changed['checkpoint_projection']['content'][0]['controls'][0]['value']=='Calgary'
    asyncio.run(run())


@pytest.mark.browser
def test_changed_network_archive_is_rejected_without_live_fallback(tmp_path):
    async def run():
        har=tmp_path/'untrusted.har';tmp_path.mkdir(exist_ok=True);har.write_text('{}')
        async with Browser(tmp_path/'browser',headless=True) as browser:
            with pytest.raises(ValueError,match='missing or changed'):
                await browser.begin_checkpoint_replay({'mode':'recorded_prefix_then_live','har_path':str(har),'sha256':'wrong'})
            assert browser.network_mode=='live_capture'
    asyncio.run(run())
