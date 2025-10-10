import os
import time
import asyncio
import requests
import subprocess
import psutil
import json
import logging
from datetime import datetime
from typing import Dict
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn

# .env oku
load_dotenv()
CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

# Cookie değerleri - sadece değerleri girin, cookie yapısını script halleder
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
LOGIN_TOKEN = os.getenv("LOGIN_TOKEN", "")
PERSISTENT_TOKEN = os.getenv("PERSISTENT_TOKEN", "")
TWILIGHT_USER = os.getenv("TWILIGHT_USER", "")

STREAMERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamers.txt")
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"

# Logger kurulumu
def setup_logger():
    logger = logging.getLogger('TwitchMonitor')
    logger.setLevel(logging.INFO)
    
    # Dosya handler
    file_handler = logging.FileHandler('twitch_monitor.log')
    file_handler.setLevel(logging.INFO)
    
    # Console handler (sadece ERROR ve üstü)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    
    # Formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

console = Console()
drivers = {}  # username -> selenium driver
watch_times = {}  # username -> start_time
logs = []
start_time = time.time()
headless_mode = True
script_process = psutil.Process()  # Current process for monitoring

# --- utils ---
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
        logger.info(f"API kontrolü tamamlandı: {len(online)} online streamer")
        return online
    except Exception as e:
        logger.error(f"Online streamer kontrolü hatası: {e}")
        return {}

# --- izleme ---
def start_selenium(user: str, headless: bool):
    try:
        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)
        
        # Audio devre dışı bırakma seçenekleri
        opts.add_argument("--mute-audio")
        opts.add_argument("--disable-audio-output")
        opts.add_argument("--disable-background-media-suspend")
        opts.add_argument("--autoplay-policy=no-user-gesture-required")
        
        # Media permissions - ses ve video erişimini engelle
        prefs = {
            "profile.default_content_setting_values.media_stream_mic": 2,
            "profile.default_content_setting_values.media_stream_camera": 2,
            "profile.default_content_settings.popups": 0,
            "profile.managed_default_content_settings.images": 2,  # Resimleri de kapatabilirsiniz (performans için)
            "profile.default_content_setting_values.notifications": 2
        }
        opts.add_experimental_option("prefs", prefs)
        
        opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(options=opts)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # İlk önce Twitch'e git
        driver.get("https://www.twitch.tv")
        time.sleep(2)
        
        # Ses ayarlarını devre dışı bırak (JavaScript ile)
        driver.execute_script("""
            // Tüm video/audio elementlerini sessiz yap
            setInterval(function() {
                var videos = document.querySelectorAll('video');
                videos.forEach(function(video) {
                    if (!video.muted) {
                        video.muted = true;
                        video.volume = 0;
                    }
                });
                
                var audios = document.querySelectorAll('audio');
                audios.forEach(function(audio) {
                    if (!audio.muted) {
                        audio.muted = true;
                        audio.volume = 0;
                    }
                });
            }, 1000);
        """)
        
        # Cookie'leri ekle (sadece değer varsa)
        cookies_added = 0
        if AUTH_TOKEN:
            driver.add_cookie({
                "name": "auth-token", 
                "value": AUTH_TOKEN, 
                "domain": ".twitch.tv", 
                "path": "/",
                "secure": True
            })
            cookies_added += 1
            
        if LOGIN_TOKEN:
            driver.add_cookie({
                "name": "login", 
                "value": LOGIN_TOKEN, 
                "domain": ".twitch.tv", 
                "path": "/",
                "secure": True
            })
            cookies_added += 1
            
        if PERSISTENT_TOKEN:
            driver.add_cookie({
                "name": "persistent", 
                "value": PERSISTENT_TOKEN, 
                "domain": ".twitch.tv", 
                "path": "/",
                "secure": True
            })
            cookies_added += 1
            
        if TWILIGHT_USER:
            driver.add_cookie({
                "name": "twilight-user", 
                "value": TWILIGHT_USER, 
                "domain": ".twitch.tv", 
                "path": "/",
                "secure": True
            })
            cookies_added += 1
        
        # Streamer sayfasına git
        driver.get(f"https://www.twitch.tv/{user}")
        
        # Sayfa yüklendikten sonra tekrar ses kontrolü yap
        time.sleep(3)
        driver.execute_script("""
            // Sayfa yüklenince tüm medya elementlerini sessiz yap
            var videos = document.querySelectorAll('video');
            videos.forEach(function(video) {
                video.muted = true;
                video.volume = 0;
            });
            
            var audios = document.querySelectorAll('audio');
            audios.forEach(function(audio) {
                audio.muted = true;
                audio.volume = 0;
            });
            
            // Twitch player'ın ses butonunu bul ve tıkla (mute)
            setTimeout(function() {
                var muteButton = document.querySelector('[data-a-target="player-mute-unmute-button"]');
                if (muteButton && muteButton.getAttribute('aria-label').includes('Unmute')) {
                    muteButton.click();
                }
            }, 2000);
        """)
        
        drivers[user] = driver
        watch_times[user] = time.time()
        
        log_message = f"[+] {user} yayını açıldı (MUTED - {'headless' if headless else 'normal'}) - {cookies_added} cookie eklendi"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")
        logger.info(f"Selenium başlatıldı - {user} - {cookies_added} cookie")
        
    except Exception as e:
        logger.error(f"Selenium başlatma hatası - {user}: {e}")
        log_message = f"[-] {user} yayını açılamadı: {str(e)}"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")

def stop_selenium(user: str):
    if user in drivers:
        try:
            drivers[user].quit()
            logger.info(f"Selenium kapatıldı - {user}")
        except Exception as e:
            logger.warning(f"Selenium kapatma hatası - {user}: {e}")
        
        if user in drivers:
            del drivers[user]
        if user in watch_times:
            del watch_times[user]
        
        log_message = f"[-] {user} yayını kapatıldı"
        logs.append(f"[{time.strftime('%H:%M:%S')}] {log_message}")

def format_minutes(seconds: float) -> str:
    return f"{int(seconds//60)} dk {int(seconds%60)} sn"

def get_system_stats():
    try:
        # System-wide stats
        uptime = int(time.time() - start_time)
        system_cpu = psutil.cpu_percent()
        system_mem = psutil.virtual_memory().percent
        
        # Script-specific stats
        try:
            script_cpu = script_process.cpu_percent()
            script_mem = script_process.memory_info().rss / 1024 / 1024  # MB
            script_mem_percent = script_process.memory_percent()
        except:
            script_cpu = 0
            script_mem = 0
            script_mem_percent = 0
        
        # GPU temperature
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
    # Sol panel: Aktif izleme durumu
    table = Table(title="[bold cyan]📺 İzleme Durumu[/]", style="bright_white")
    table.add_column("Streamer", style="bold yellow")
    table.add_column("Süre", style="bold green")
    table.add_column("Durum", style="bold blue")
    
    if drivers:
        for user in drivers:
            elapsed = time.time() - watch_times[user]
            status = "🔴 CANLI" if user in online_streamers else "⚫ OFFLINE"
            table.add_row(user, format_minutes(elapsed), status)
    else:
        table.add_row("---", "---", "Henüz yayın yok")
    
    left_panel = Panel(table, title="[bold blue]Aktif Yayınlar[/]")
    
    # Orta panel: Sistem durumu ve streamer listesi
    uptime, sys_cpu, sys_mem, gpu_temp, script_cpu, script_mem_mb, script_mem_percent = get_system_stats()
    
    # Cookie durumu kontrolü
    cookie_count = sum([1 for token in [AUTH_TOKEN, LOGIN_TOKEN, PERSISTENT_TOKEN, TWILIGHT_USER] if token])
    cookie_status = f"✅ {cookie_count}/4" if cookie_count > 0 else "❌ 0/4"
    
    # Sistem istatistikleri tablosu
    sys_table = Table(title="[bold magenta]🖥 Sistem & Script İstatistikleri[/]", style="bright_white")
    sys_table.add_column("Kategori", style="bold cyan")
    sys_table.add_column("Değer", style="bold green")
    
    # Script stats
    sys_table.add_row("Script Uptime", f"{uptime//3600}s {(uptime%3600)//60}dk {uptime%60}sn")
    sys_table.add_row("Script CPU", f"{script_cpu:.1f}%")
    sys_table.add_row("Script RAM", f"{script_mem_mb:.1f}MB ({script_mem_percent:.1f}%)")
    sys_table.add_row("Aktif Yayın", f"{len(drivers)}")
    sys_table.add_row("Cookie Durumu", cookie_status)
    sys_table.add_row("", "")  # Separator
    
    # System stats
    sys_table.add_row("Sistem CPU", f"{sys_cpu:.1f}%")
    sys_table.add_row("Sistem RAM", f"{sys_mem:.1f}%")
    sys_table.add_row("GPU Temp", f"{gpu_temp}")
    
    # Streamer durumu tablosu
    streamer_table = Table(title="[bold green]📋 Streamer Durumları[/]", style="bright_white")
    streamer_table.add_column("Streamer", style="bold yellow")
    streamer_table.add_column("Durum", style="bold blue")
    streamer_table.add_column("İzleniyor", style="bold green")
    
    for streamer in sorted(all_streamers):
        if streamer in online_streamers:
            status = "🔴 ONLINE"
        else:
            status = "⚫ OFFLINE"
        
        watching = "✅ EVET" if streamer in drivers else "❌ HAYIR"
        streamer_table.add_row(streamer, status, watching)
    
    # Progress barları
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
    
    # Panel içeriğini Group olarak oluştur
    from rich.console import Group
    
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
    
    # Sağ panel: Loglar
    log_text = "\n".join(logs[-20:]) if logs else "Henüz log yok."
    right_panel = Panel(log_text, title="[bold red]📜 Sistem Logları[/]", height=25)
    
    return left_panel, middle_panel, right_panel

async def monitor_streams():
    global headless_mode
    
    # Başlangıçta headless modunu sor
    console.print("[bold yellow]🎮 Twitch Stream Monitor v2.0[/]")
    console.print(f"[cyan]Cookie Durumu: {sum([1 for token in [AUTH_TOKEN, LOGIN_TOKEN, PERSISTENT_TOKEN, TWILIGHT_USER] if token])}/4[/]")
    
    headless_input = console.input("[cyan]Headless modda çalıştır? (y/n): [/]").strip().lower()
    headless_mode = headless_input == "y"
    
    logs.append(f"[{time.strftime('%H:%M:%S')}] Monitor başlatıldı - Headless: {headless_mode}")
    logger.info(f"Monitor başlatıldı - Headless: {headless_mode}")
    
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
                # Her 60 saniyede bir API kontrolü yap
                if check_counter % 60 == 0:
                    try:
                        online = get_online_streamers(streamers, token)
                        logs.append(f"[{time.strftime('%H:%M:%S')}] API kontrolü: {len(online)} online streamer")
                        
                        # Yeni online olanları aç
                        for u in online:
                            if u not in drivers:
                                start_selenium(u, headless_mode)
                        
                        # Offline olanları kapat
                        for u in list(drivers.keys()):
                            if u not in online:
                                stop_selenium(u)
                                
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
                    # API kontrolü yapmadığımız durumlarda online listesini güncelle
                    try:
                        online = get_online_streamers(list(drivers.keys()), token) if drivers else {}
                    except:
                        online = {}
                
                # Panelleri güncelle
                left, middle, right = render_panels(online, streamers)
                layout["left"].update(left)
                layout["middle"].update(middle)
                layout["right"].update(right)
                
                check_counter += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Ana döngü hatası: {e}")
                logs.append(f"[{time.strftime('%H:%M:%S')}] Ana döngü hatası: {str(e)}")
                await asyncio.sleep(5)  # Hata durumunda 5 saniye bekle

def cleanup():
    """Temizlik işlemleri"""
    logger.info("Temizlik işlemleri başlatılıyor...")
    for user in list(drivers.keys()):
        stop_selenium(user)
    logs.append(f"[{time.strftime('%H:%M:%S')}] Tüm yayınlar kapatıldı")
    logger.info("Tüm yayınlar kapatıldı")

async def main():
    try:
        logger.info("Twitch Monitor başlatılıyor...")
        await monitor_streams()
    except KeyboardInterrupt:
        console.print("\n[bold red]Program sonlandırılıyor...[/]")
        logger.info("Program kullanıcı tarafından sonlandırıldı")
        cleanup()
        console.print("[bold green]Program temiz bir şekilde sonlandırıldı.[/]")
        logger.info("Program temiz bir şekilde sonlandırıldı")
    except Exception as e:
        console.print(f"\n[bold red]Beklenmeyen hata: {e}[/]")
        logger.error(f"Beklenmeyen hata: {e}")
        cleanup()

if __name__ == "__main__":
    asyncio.run(main())