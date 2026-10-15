"""MAB profile manager built on Playwright."""
from __future__ import annotations
import asyncio, json, os, re, time, uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

@dataclass
class MABProfile:
    id: str
    name: str
    site: str
    account_label: str
    status: str
    created_at: float
    last_used: float | None = None
    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

def clean_site(value: str) -> str:
    parsed = urlsplit(str(value or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('site must be a valid http(s) URL without embedded credentials')
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or '/', '', ''))

def safe_url(value: str) -> str:
    parsed = urlsplit(str(value or ''))
    if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    return 'about:blank'

def redact_visible_text(value: str, limit: int = 8000) -> str:
    pattern = re.compile(r'(?i)\b(cookie|password|authorization|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*[^\s,;]+')
    return pattern.sub(lambda match: match.group(1) + '=[redacted]', str(value or ''))[:limit]

class MABManager:
    def __init__(self):
        root = Path(os.environ.get('VEYA_HOME') or (Path.home() / '.veya'))
        self.base_dir = root / 'mab'
        self.profiles_dir = self.base_dir / 'profiles'
        self.max_instances = max(1, int(os.environ.get('VEYA_MAB_MAX_INSTANCES', '3')))
        self.idle_timeout = max(1.0, float(os.environ.get('VEYA_MAB_IDLE_TIMEOUT_SECONDS', '300')))
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_factory = None
        self._lock = asyncio.Lock()
        self._loop = None
        self._reaper_task = None

    def _secure_dir(self, path: Path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    def _ensure_storage(self):
        self._secure_dir(self.base_dir)
        self._secure_dir(self.profiles_dir)

    def _profile_dir(self, pid: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{32}', str(pid)): raise ValueError('invalid profile id')
        root=self.profiles_dir.resolve(); path=(self.profiles_dir/pid).resolve()
        if path.parent != root: raise ValueError('profile path escapes storage root')
        return path
    def _read(self, path: Path):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            fields={'id','name','site','account_label','status','created_at','last_used'}
            return MABProfile(**data) if set(data)==fields else None
        except (OSError,ValueError,TypeError,json.JSONDecodeError): return None
    def _write(self,p:MABProfile):
        self._ensure_storage(); folder=self._profile_dir(p.id)
        self._secure_dir(folder); self._secure_dir(folder/'browser-data')
        tmp=folder/'profile.json.tmp'; final=folder/'profile.json'
        tmp.write_text(json.dumps(p.public_dict(),ensure_ascii=False,indent=2),encoding='utf-8')
        os.chmod(tmp,0o600); tmp.replace(final); os.chmod(final,0o600)
    def list_profiles(self):
        self._ensure_storage(); result=[]
        for path in sorted(self.profiles_dir.glob('*/profile.json')):
            profile=self._read(path)
            if profile: result.append(profile)
        return result
    def get_profile(self,key:str):
        key=str(key or '').strip()
        if not key: return None
        try:
            path=self._profile_dir(key)/'profile.json'
            if path.is_file(): return self._read(path)
        except ValueError: pass
        return next((p for p in self.list_profiles() if p.name.casefold()==key.casefold()),None)
    def create_profile(self,name:str,site:str,account_label:str='default'):
        name=str(name).strip(); label=str(account_label or 'default').strip(); site=clean_site(site)
        if not name or len(name)>120: raise ValueError('profile name must be 1-120 characters')
        if not label or len(label)>120: raise ValueError('account label must be 1-120 characters')
        if any(p.name.casefold()==name.casefold() and p.site==site for p in self.list_profiles()):
            raise ValueError('profile name and site already exist')
        p=MABProfile(uuid.uuid4().hex,name,site,label,'active',time.time(),None); self._write(p); return p
    def update_profile(self,pid:str,*,name=None,site=None,account_label=None,status=None):
        p=self.get_profile(pid)
        if p is None: return None
        if name is not None:
            value=str(name).strip()
            if not value or len(value)>120: raise ValueError('profile name must be 1-120 characters')
            p.name=value
        if site is not None: p.site=clean_site(site)
        if account_label is not None:
            value=str(account_label).strip()
            if not value or len(value)>120: raise ValueError('account label must be 1-120 characters')
            p.account_label=value
        if status is not None:
            if status not in {'active','disabled'}: raise ValueError('status must be active or disabled')
            p.status=status
        self._write(p); return p
    def touch_profile(self,pid:str):
        p=self.get_profile(pid)
        if p is None: return None
        p.last_used=time.time(); self._write(p)
        if p.id in self._sessions: self._sessions[p.id]['last_activity']=time.monotonic()
        return p
    def delete_profile(self,pid:str,*,confirm=False):
        if not confirm: raise ValueError('confirm=true is required to delete a profile')
        p=self.get_profile(pid)
        if p is None: return False
        if p.id in self._sessions:
            if self._loop is None or not self._loop.is_running(): raise ValueError('browser session is active')
            asyncio.run_coroutine_threadsafe(self.close_session(p.id),self._loop).result(timeout=10)
        folder=self._profile_dir(p.id)
        if folder.exists():
            for child in folder.rglob('*'):
                if child.is_file() or child.is_symlink(): child.unlink()
            for child in sorted(folder.rglob('*'),key=lambda item:len(item.parts),reverse=True):
                if child.is_dir(): child.rmdir()
            folder.rmdir()
        return True
    async def _start_session(self,p):
        data_dir=self._profile_dir(p.id)/'browser-data'; self._secure_dir(data_dir)
        if self._session_factory is not None: return await self._session_factory(p)
        try: from playwright.async_api import async_playwright
        except ImportError as exc: raise RuntimeError('Playwright runtime unavailable') from exc
        pw=await async_playwright().start()
        try:
            context=await pw.chromium.launch_persistent_context(user_data_dir=str(data_dir),
                headless=os.environ.get('VEYA_MAB_HEADLESS','1').lower() not in {'0','false','no'},
                viewport={'width':1280,'height':800},args=['--remote-debugging-address=127.0.0.1'])
            page=context.pages[0] if context.pages else await context.new_page()
            if not page.url or page.url=='about:blank': await page.goto(p.site,wait_until='domcontentloaded',timeout=20000)
            return {'playwright':pw,'context':context,'page':page,'last_activity':time.monotonic()}
        except Exception:
            await pw.stop(); raise
    async def ensure_browser(self,key):
        p=self.get_profile(key)
        if p is None: raise ValueError('profile not found')
        if p.status!='active': raise PermissionError('profile is disabled/revoked')
        self._loop=asyncio.get_running_loop()
        if p.id in self._sessions:
            self._sessions[p.id]['last_activity']=time.monotonic(); self.touch_profile(p.id); return self._sessions[p.id]
        async with self._lock:
            if p.id in self._sessions: return self._sessions[p.id]
            await self.expire_idle()
            if len(self._sessions)>=self.max_instances: raise RuntimeError(f'instance limit reached ({self.max_instances})')
            session=await self._start_session(p); session.setdefault('last_activity',time.monotonic())
            self._sessions[p.id]=session; self.touch_profile(p.id)
            if self._reaper_task is None or self._reaper_task.done(): self._reaper_task=asyncio.create_task(self._idle_reaper())
            return session
    async def close_session(self,pid):
        session=self._sessions.pop(pid,None)
        if not session: return
        try:
            if session.get('context') is not None: await session['context'].close()
        except Exception: pass
        try:
            if session.get('playwright') is not None: await session['playwright'].stop()
        except Exception: pass
    async def expire_idle(self):
        now=time.monotonic()
        expired=[pid for pid,s in list(self._sessions.items()) if now-float(s.get('last_activity',now))>=self.idle_timeout]
        for pid in expired: await self.close_session(pid)
        return expired
    async def _idle_reaper(self):
        try:
            while self._sessions:
                await asyncio.sleep(min(15.0,max(.25,self.idle_timeout/4)))
                await self.expire_idle()
        except asyncio.CancelledError: raise
    async def perform_action(self,key,action,*,url=None,selector=None,text=None):
        if action not in {'navigate','click','type','snapshot','back','forward','reload'}:
            raise ValueError('unsupported browser action')
        session=await self.ensure_browser(key); page=session['page']
        if action=='navigate': await page.goto(clean_site(url or ''),wait_until='domcontentloaded',timeout=20000)
        elif action=='click':
            if not selector or len(selector)>500: raise ValueError('click requires a CSS selector')
            await page.locator(selector).first.click(timeout=5000)
        elif action=='type':
            if not selector or len(selector)>500: raise ValueError('type requires a CSS selector')
            await page.locator(selector).first.fill(str(text or ''),timeout=5000)
        elif action=='back': await page.go_back(wait_until='domcontentloaded',timeout=15000)
        elif action=='forward': await page.go_forward(wait_until='domcontentloaded',timeout=15000)
        elif action=='reload': await page.reload(wait_until='domcontentloaded',timeout=15000)
        p=self.get_profile(key); self.touch_profile(p.id); session['last_activity']=time.monotonic()
        result={'success':True,'profile':p.name,'url':safe_url(page.url),'title':redact_visible_text(await page.title(),300)}
        if action=='snapshot':
            try: body=await page.locator('body').inner_text(timeout=5000)
            except Exception: body=''
            result['text']=redact_visible_text(body)
        return result

mab_manager=MABManager()
