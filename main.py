import os
import time
import asyncio
import requests
import psutil
import json
import logging
from datetime import datetime
from typing import Dict
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn
from rich.console import Group
from aiohttp import web

load_dotenv()
CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
LOGIN_TOKEN = os.getenv("LOGIN_TOKEN", "")
PERSISTENT_TOKEN = os.getenv("PERSISTENT_TOKEN", "")
TWILIGHT_USER = os.getenv("TWILIGHT_USER", "")

HEADLESS_MODE = os.getenv("HEADLESS", "true").lower() in ["true", "1", "yes"]
AUTO_START = os.getenv("AUTO_START", "true").lower() in ["true", "1", "yes"]  # Server için true

STREAMERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamers.txt")
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"

def get_html_status(online_streamers, all_streamers):
    left, middle, right = render_panels(online_streamers, all_streamers)
    tmp_console = Console(record=True, width=120)
    tmp_console.print(left)
    tmp_console.print(middle)
    tmp_console.print(right)
    return tmp_console.export_html(inline_styles=True)

async def keep_alive_handler(request):
    return web.Response(text=f"Twitch Monitor is alive. Uptime: {format_minutes(time.time() - start_time)}")

async def status_handler(request):
    try:
        token = get_app_token()
        streamers = read_streamers(STREAMERS_FILE)
        online = get_online_streamers(streamers, token)
        html = get_html_status(online, streamers)
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {e}", content_type="text/plain")

async def start_keep_alive_server():
    host = '0.0.0.0'
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.add_routes([
        web.get('/', keep_alive_handler),
        web.get('/status', status_handler)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    logger.info(f"Keep-Alive Web Server başlatılıyor: http://{host}:{port}")
    await site.start()

def setup_logger():
    logger = logging.getLogger('TwitchMonitor')
    logger.setLevel(logging.INFO)
    
    file_handler = logging.FileHandler('twitch_monitor.log')
    file_handler.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

console = Console()
playwright = None
browser = None
contexts = {}
pages = {}
watch_times = {}
logs = []
start_time = time.time()
headless_mode = True
script_process = psutil.Process()

def get_app_token() -> str:
    try:
        resp = requests.post(
            TWITCH_TOKEN_URL,
            data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Twitch API token başarıyla alındı")
        return resp.json()["access_token"]
    except Exception as e:
        logger.error(f"Twitch API token alma hatası: {e}")
        raise

def read_streamers(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            streamers = [l.strip().lower() for l in f if l.strip()]
        logger.info(f"{len(streamers)} streamer streamers.txt'den yüklendi")
        return streamers
    except FileNotFoundError:
        logger.error(f"streamers.txt dosyası bulunamadı: {path}")
        return []
    except Exception as e:
        logger.error(f"streamers.txt okuma hatası: {e}")
        return []

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def get_online_streamers(usernames, token) -> Dict[str, dict]:
    headers = {"Authorization": f"Bearer {token}", "Client-Id": CLIENT_ID}
    online = {}
    try:
        for chunk in chunked(usernames, 100):
            params = [("user_login", u) for u in chunk]
            resp = requests.get(TWITCH_STREAMS_URL, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                user = item["user_login"].lower()
                online[user] = item
        return online
    except Exception as e:
        logger.error(f"Online streamer kontrolü hatası: {e}")
        return {}

async def init_playwright():
    global playwright, browser
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=headless_mode,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage", 
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--autoplay-policy=no-user-gesture-required"
            ]
        )
        logger.info("Playwright başarıyla başlatıldı")
        return True
    except Exception as e:
        logger.error(f"Playwright başlatma hatası: {e}")
        return False

async def start_watching(user: str):
    try:
        if not browser:
            logger.error("Browser başlatılmamış")
            return False

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 400, "height": 300},
            ignore_https_errors=True
        )
        
        cookies_to_add = []
        if AUTH_TOKEN:
            cookies_to_add.append({
                "name": "auth-token",
                "value": AUTH_TOKEN,
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True
            })
        if LOGIN_TOKEN:
            cookies_to_add.append({
                "name": "login", 
                "value": LOGIN_TOKEN,
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True
            })
        if PERSISTENT_TOKEN:
            cookies_to_add.append({
                "name": "persistent",
                "value": PERSISTENT_TOKEN, 
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True
            })
        if TWILIGHT_USER:
            cookies_to_add.append({
                "name": "twilight-user",
                "value": TWILIGHT_USER,
                "domain": ".twitch.tv", 
                "path": "/",
                "secure": True
            })
        
        if cookies_to_add:
            await context.add_cookies(cookies_to_add)
        
        page = await context.new_page()
        
        # KRITIK: Video/Audio'yu tamamen engelle - kaynak tüketimini minimize et
        await page.route("**/*.m3u8", lambda route: route.abort())
        await page.route("**/*.ts", lambda route: route.abort())
        await page.route("**/*.mp4", lambda route: route.abort())
        await page.route("**/*.webm", lambda route: route.abort())
        
        # Video elementlerini devre dışı bırak
        await page.add_init_script("""
            // Video ve audio elementlerini tamamen engelle
            const originalCreateElement = document.createElement.bind(document);
            document.createElement = function(tagName) {
                const element = originalCreateElement(tagName);
                if (tagName.toLowerCase() === 'video' || tagName.toLowerCase() === 'audio') {
                    // Video/Audio elementini oluştur ama hiçbir şey yükleme
                    element.play = () => Promise.resolve();
                    element.pause = () => {};
                    element.load = () => {};
                    Object.defineProperty(element, 'src', {
                        set: () => {},
                        get: () => ''
                    });
                    Object.defineProperty(element, 'currentTime', {
                        set: () => {},
                        get: () => 0
                    });
                    Object.defineProperty(element, 'volume', {
                        set: () => {},
                        get: () => 0
                    });
                    Object.defineProperty(element, 'muted', {
                        set: () => {},
                        get: () => true
                    });
                }
                return element;
            };
            
            // MediaSource'u engelle
            window.MediaSource = class {
                constructor() { throw new Error('Blocked'); }
            };
            
            // Audio Context'i engelle
            window.AudioContext = class { constructor() { throw new Error('Blocked'); } };
            window.webkitAudioContext = class { constructor() { throw new Error('Blocked'); } };
            
            // Her 2 saniyede bir kontrol et
            setInterval(() => {
                document.querySelectorAll('video, audio').forEach(e => {
                    e.pause();
                    e.src = '';
                    e.load = () => {};
                    e.muted = true;
                    e.volume = 0;
                });
            }, 2000);
        """)
        
        try:
            await page.goto(f"https://www.twitch.tv/{user}", wait_until="domcontentloaded", timeout=15000)
        except:
            try:
                await page.goto(f"https://www.twitch.tv/{user}", timeout=10000)
            except:
                pass
        
        await asyncio.sleep(2)
        # "İzlemeye Başla" butonuna otomatik tıkla
                # "İzlemeye Başla" butonuna otomatik tıkla
        try:
            await page.wait_for_selector('div[data-a-target="tw-core-button-label-text"]', timeout=5000)
            buttons = await page.query_selector_all('div[data-a-target="tw-core-button-label-text"]')
            for b in buttons:
                text = (await b.inner_text()).strip().lower()
                if "izlemeye başla" in text:
                    await b.click()
                    logger.info(f"{user} yayını için 'İzlemeye Başla' butonuna otomatik basıldı")
                    break
        except Exception as e:
            logger.warning(f"{user} yayını için 'İzlemeye Başla' butonu bulunamadı veya tıklanamadı: {e}")

	
        # Sayfayı minimum kaynak kullanacak şekilde ayarla
        try:
            await page.evaluate("""
                // Tüm video ve audio elementlerini tamamen durdur
                document.querySelectorAll('video, audio').forEach(e => {
                    e.pause();
                    e.src = '';
                    e.srcObject = null;
                    e.muted = true;
                    e.volume = 0;
                    e.removeAttribute('src');
                });
                
                // Player'ı kontrol et ve durdur
                const player = document.querySelector('[data-a-target="video-player"]');
                if (player) {
                    player.style.pointerEvents = 'none';
                }
            """)
        except:
            pass
        
        contexts[user] = context
        pages[user] = page
        watch_times[user] = time.time()
        
        cookies_added = len(cookies_to_add)
        log_message = f"[+] {user} yayını açıldı (NO STREAM - kaynak tasarrufu) - {cookies_added} cookie eklendi"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")
        logger.info(f"Playwright başlatıldı - {user} - {cookies_added} cookie - NO STREAM MODE")
        
        return True
        
    except Exception as e:
        logger.error(f"Playwright başlatma hatası - {user}: {e}")
        log_message = f"[-] {user} yayını açılamadı: {str(e)}"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")
        return False

async def stop_watching(user: str):
    try:
        if user in pages:
            try:
                await pages[user].close()
            except:
                pass
            del pages[user]
        
        if user in contexts:
            try:
                await contexts[user].close()
            except:
                pass
            del contexts[user]
            
        if user in watch_times:
            del watch_times[user]
        
        log_message = f"[-] {user} yayını kapatıldı"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")
        logger.info(f"Playwright kapatıldı - {user}")
        
    except Exception as e:
        logger.warning(f"Playwright kapatma hatası - {user}: {e}")

def format_minutes(seconds: float) -> str:
    return f"{int(seconds//60)} dk {int(seconds%60)} sn"

def get_system_stats():
    try:
        uptime = int(time.time() - start_time)
        system_cpu = psutil.cpu_percent()
        system_mem = psutil.virtual_memory().percent
        
        try:
            script_cpu = script_process.cpu_percent()
            script_mem = script_process.memory_info().rss / 1024 / 1024
            script_mem_percent = script_process.memory_percent()
        except:
            script_cpu = 0
            script_mem = 0
            script_mem_percent = 0
        
        try:
            gpu_temp = psutil.sensors_temperatures().get("gpu", [{}])[0].get("current", "N/A")
        except:
            gpu_temp = "N/A"
        
        return uptime, system_cpu, system_mem, gpu_temp, script_cpu, script_mem, script_mem_percent
    except Exception as e:
        logger.error(f"Sistem istatistikleri alma hatası: {e}")
        return 0, 0, 0, "N/A", 0, 0, 0

def make_layout():
    layout = Layout()
    layout.split_row(
        Layout(name="left", ratio=2),
        Layout(name="middle", ratio=2),
        Layout(name="right", ratio=3),
    )
    return layout

def render_panels(online_streamers, all_streamers):
    table = Table(title="[bold cyan]📺 İzleme Durumu[/]", style="bright_white")
    table.add_column("Streamer", style="bold yellow")
    table.add_column("Süre", style="bold green")
    table.add_column("Durum", style="bold blue")
    
    if pages:
        for user in pages:
            elapsed = time.time() - watch_times[user]
            status = "🔴 CANLI" if user in online_streamers else "⚫ OFFLINE"
            table.add_row(user, format_minutes(elapsed), status)
    else:
        table.add_row("---", "---", "Henüz yayın yok")
    
    left_panel = Panel(table, title="[bold blue]Aktif Yayınlar[/]")
    
    uptime, sys_cpu, sys_mem, gpu_temp, script_cpu, script_mem_mb, script_mem_percent = get_system_stats()
    
    cookie_count = sum([1 for token in [AUTH_TOKEN, LOGIN_TOKEN, PERSISTENT_TOKEN, TWILIGHT_USER] if token])
    cookie_status = f"✅ {cookie_count}/4" if cookie_count > 0 else "❌ 0/4"
    
    sys_table = Table(title="[bold magenta]🖥 Sistem & Script İstatistikleri[/]", style="bright_white")
    sys_table.add_column("Kategori", style="bold cyan")
    sys_table.add_column("Değer", style="bold green")
    
    sys_table.add_row("Script Uptime", f"{uptime//3600}s {(uptime%3600)//60}dk {uptime%60}sn")
    sys_table.add_row("Script CPU", f"{script_cpu:.1f}%")
    sys_table.add_row("Script RAM", f"{script_mem_mb:.1f}MB ({script_mem_percent:.1f}%)")
    sys_table.add_row("Aktif Yayın", f"{len(pages)}")
    sys_table.add_row("Cookie Durumu", cookie_status)
    sys_table.add_row("", "")
    
    sys_table.add_row("Sistem CPU", f"{sys_cpu:.1f}%")
    sys_table.add_row("Sistem RAM", f"{sys_mem:.1f}%")
    sys_table.add_row("GPU Temp", f"{gpu_temp}")
    
    streamer_table = Table(title="[bold green]📋 Streamer Durumları[/]", style="bright_white")
    streamer_table.add_column("Streamer", style="bold yellow")
    streamer_table.add_column("Durum", style="bold blue")
    streamer_table.add_column("İzleniyor", style="bold green")
    
    for streamer in sorted(all_streamers):
        if streamer in online_streamers:
            status = "🔴 ONLINE"
        else:
            status = "⚫ OFFLINE"
        
        watching = "✅ EVET" if streamer in pages else "❌ HAYIR"
        streamer_table.add_row(streamer, status, watching)
    
    sys_cpu_bar = Progress(
        TextColumn("Sys CPU", style="bold cyan"),
        BarColumn(bar_width=12, complete_style="red"),
        TextColumn(f"{sys_cpu:.1f}%", style="bold white"),
    )
    sys_mem_bar = Progress(
        TextColumn("Sys RAM", style="bold cyan"),
        BarColumn(bar_width=12, complete_style="green"),
        TextColumn(f"{sys_mem:.1f}%", style="bold white"),
    )
    script_cpu_bar = Progress(
        TextColumn("Script CPU", style="bold yellow"),
        BarColumn(bar_width=12, complete_style="orange1"),
        TextColumn(f"{script_cpu:.1f}%", style="bold white"),
    )
    script_mem_bar = Progress(
        TextColumn("Script RAM", style="bold yellow"),
        BarColumn(bar_width=12, complete_style="blue"),
        TextColumn(f"{script_mem_percent:.1f}%", style="bold white"),
    )
    
    sys_cpu_bar.add_task("sys_cpu", total=100, completed=sys_cpu)
    sys_mem_bar.add_task("sys_mem", total=100, completed=sys_mem)
    script_cpu_bar.add_task("script_cpu", total=100, completed=script_cpu)
    script_mem_bar.add_task("script_mem", total=100, completed=script_mem_percent)
    
    middle_content = Group(
        sys_table,
        "",
        sys_cpu_bar,
        sys_mem_bar, 
        script_cpu_bar,
        script_mem_bar,
        "",
        streamer_table
    )
    
    middle_panel = Panel(middle_content, title="[bold magenta]Sistem Durumu[/]")
    
    log_text = "\n".join(logs[-20:]) if logs else "Henüz log yok."
    right_panel = Panel(log_text, title="[bold red]📜 Sistem Logları[/]", height=25)
    
    return left_panel, middle_panel, right_panel

def is_interactive():
    import sys
    return hasattr(sys.stdin, 'isatty') and sys.stdin.isatty()

async def monitor_streams():
    global headless_mode
    
    console.print("[bold yellow]🎮 Twitch Stream Monitor v3.1 (Optimized - No Stream)[/]")
    console.print(f"[cyan]Cookie Durumu: {sum([1 for token in [AUTH_TOKEN, LOGIN_TOKEN, PERSISTENT_TOKEN, TWILIGHT_USER] if token])}/4[/]")
    
    if AUTO_START or not is_interactive():
        headless_mode = HEADLESS_MODE
        mode_text = "headless" if headless_mode else "görsel"
        console.print(f"[cyan]Otomatik başlatma - {mode_text} modda çalışıyor (Server Mode)[/]")
    else:
        try:
            default_text = "y" if HEADLESS_MODE else "n"
            headless_input = console.input(f"[cyan]Headless modda çalıştır? (y/n) [varsayılan: {default_text}]: [/]").strip().lower()
            if not headless_input:
                headless_input = default_text
            headless_mode = headless_input == "y"
        except (EOFError, OSError):
            headless_mode = HEADLESS_MODE
            console.print(f"[cyan]Input alınamadı - Environment variable kullanılıyor: {'headless' if headless_mode else 'görsel'}[/]")
        except KeyboardInterrupt:
            console.print("\n[bold red]Program iptal edildi.[/]")
            return
    
    logs.append(f"[{time.strftime('%H:%M:%S')}] Monitor başlatıldı - Headless: {headless_mode} - NO STREAM MODE")
    logger.info(f"Monitor başlatıldı - Headless: {headless_mode} - NO STREAM MODE")
    
    if not await init_playwright():
        console.print("[bold red]❌ Playwright başlatılamadı![/]")
        return
    
    try:
        token = get_app_token()
        streamers = read_streamers(STREAMERS_FILE)
        
        if not streamers:
            console.print("[bold red]❌ Streamer listesi yüklenemedi![/]")
            return
        
        logs.append(f"[{time.strftime('%H:%M:%S')}] {len(streamers)} streamer yüklendi")
    except Exception as e:
        console.print(f"[bold red]❌ Başlangıç hatası: {e}[/]")
        logger.error(f"Başlangıç hatası: {e}")
        return
    
    layout = make_layout()
    
    with Live(layout, refresh_per_second=2, screen=True):
        check_counter = 0
        while True:
            try:
                if check_counter % 60 == 0:
                    try:
                        online = get_online_streamers(streamers, token)
                        logs.append(f"[{time.strftime('%H:%M:%S')}] API kontrolü: {len(online)} online streamer")
                        
                        new_streamers = [u for u in online if u not in pages]
                        if new_streamers:
                            logs.append(f"[{time.strftime('%H:%M:%S')}] {len(new_streamers)} streamer açılıyor...")
                            tasks = [start_watching(user) for user in new_streamers]
                            try:
                                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30.0)
                                successful = sum(1 for r in results if r is True)
                                logs.append(f"[{time.strftime('%H:%M:%S')}] {successful}/{len(new_streamers)} streamer açıldı")
                            except asyncio.TimeoutError:
                                logs.append(f"[{time.strftime('%H:%M:%S')}] Açılma timeout (30s)")
                        
                        offline_streamers = [u for u in list(pages.keys()) if u not in online]
                        if offline_streamers:
                            close_tasks = [stop_watching(user) for user in offline_streamers]
                            await asyncio.gather(*close_tasks, return_exceptions=True)
                                
                    except Exception as e:
                        logs.append(f"[{time.strftime('%H:%M:%S')}] API hatası: {str(e)}")
                        logger.error(f"API hatası: {e}")
                        try:
                            token = get_app_token()
                            logs.append(f"[{time.strftime('%H:%M:%S')}] Token yenilendi")
                        except Exception as token_error:
                            logs.append(f"[{time.strftime('%H:%M:%S')}] Token yenilenme hatası: {str(token_error)}")
                            logger.error(f"Token yenilenme hatası: {token_error}")
                            
                else:
                    try:
                        online = get_online_streamers(list(pages.keys()), token) if pages else {}
                    except:
                        online = {}
                
                left, middle, right = render_panels(online, streamers)
                layout["left"].update(left)
                layout["middle"].update(middle)
                layout["right"].update(right)
                
                check_counter += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Ana döngü hatası: {e}")
                logs.append(f"[{time.strftime('%H:%M:%S')}] Ana döngü hatası: {str(e)}")
                await asyncio.sleep(5)

async def cleanup():
    logger.info("Temizlik işlemleri başlatılıyor...")
    
    for user in list(pages.keys()):
        await stop_watching(user)
    
    if browser:
        await browser.close()
    
    if playwright:
        await playwright.stop()
    
    logs.append(f"[{time.strftime('%H:%M:%S')}] Tüm yayınlar kapatıldı")
    logger.info("Tüm yayınlar kapatıldı")

async def main():
    try:
        logger.info("Twitch Monitor başlatılıyor...")
        
        server_task = asyncio.create_task(start_keep_alive_server())
        monitor_task = asyncio.create_task(monitor_streams())
        
        await asyncio.gather(server_task, monitor_task)
        
    except KeyboardInterrupt:
        console.print("\n[bold red]Program sonlandırılıyor...[/]")
        logger.info("Program kullanıcı tarafından sonlandırıldı")
        await cleanup()
        console.print("[bold green]Program temiz bir şekilde sonlandırıldı.[/]")
        logger.info("Program temiz bir şekilde sonlandırıldı")
    except Exception as e:
        console.print(f"\n[bold red]Beklenmeyen hata: {e}[/]")
        logger.error(f"Beklenmeyen hata: {e}")
        await cleanup()

if __name__ == "__main__":
    psutil.cpu_percent()
    asyncio.run(main())
