import os
import sys
import json
import time
import re
import secrets
import string
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
import docker

# ---------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

CONFIG_FILE = "config.json"
DB_FILE = "vps_db.json"

# Load Configuration
if not os.path.exists(CONFIG_FILE):
    default_config = {
        "TOKEN": "YOUR_DISCORD_BOT_TOKEN_HERE",
        "PREFIX": "$",
        "ADMIN_IDS": [],
        "DEFAULT_DATA_DIR": "./vps_data"
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=4)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

PREFIX = config.get("PREFIX", "$")
BOT_NAME = "SDT-BOTV2"
DEFAULT_DATA_DIR = config.get("DEFAULT_DATA_DIR", "./vps_data")

# ---------------------------------------------------------
# DATABASE ENGINE
# ---------------------------------------------------------
def default_db():
    return {
        "vps": {},
        "admins": [int(x) for x in config.get("ADMIN_IDS", []) if str(x).isdigit()]
    }

def load_db():
    if not os.path.exists(DB_FILE):
        data = default_db()
        save_db(data)
        return data

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = default_db()

    defaults = default_db()
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
    return data

def save_db(data):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, DB_FILE)

# ---------------------------------------------------------
# DOCKER INITIALIZATION
# ---------------------------------------------------------
try:
    docker_client = docker.from_env()
    docker_client.ping()
    logging.info("Connected to Docker daemon successfully.")
except Exception as err:
    logging.error(f"Failed to connect to Docker daemon: {err}")
    docker_client = None

# ---------------------------------------------------------
# DISCORD INITIALIZATION
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

def generate_password(length=14):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_size_to_bytes(size_str: str) -> int:
    size_str = str(size_str).lower().strip()
    match = re.fullmatch(r"(\d+)([mg])", size_str)
    if not match:
        raise ValueError("Invalid size format. Use `1g`, `512m`, etc.")
    num, unit = match.groups()
    return int(num) * (1024 ** 2 if unit == "m" else 1024 ** 3)

def is_admin():
    async def predicate(ctx):
        db = load_db()
        admins = {int(x) for x in db.get("admins", []) if str(x).isdigit()}
        return ctx.author.id in admins or ctx.author.guild_permissions.administrator
    return commands.check(predicate)

def get_normalized_os(os_input: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", os_input).upper()
    if "UBUNTU" in cleaned:
        return "ubuntu:22.04"
    elif "DEBIAN" in cleaned:
        return "debian:12"
    return "ubuntu:22.04"

# ---------------------------------------------------------
# RELIABLE PROVISIONING ENGINE
# ---------------------------------------------------------
def _provision_vps_sync(image_tag: str, ram_bytes: int, cpu_cores: int, container_name: str, root_password: str):
    if docker_client is None:
        raise RuntimeError("Docker daemon is offline.")

    # 1. Pull Image locally or from Docker Hub
    try:
        docker_client.images.get(image_tag)
    except docker.errors.ImageNotFound:
        logging.info(f"Image {image_tag} not found locally. Pulling from Docker Hub...")
        docker_client.images.pull(image_tag)

    data_dir = os.path.abspath(DEFAULT_DATA_DIR)
    os.makedirs(os.path.join(data_dir, container_name), exist_ok=True)
    nano_cpus = int(cpu_cores * 1_000_000_000)

    # Boot command: sets root password, starts SSH server, & sets up Tailscale
    setup_script = (
        f"echo 'root:{root_password}' | chpasswd && "
        "apt-get update -y && apt-get install -y curl openssh-server && "
        "mkdir -p /var/run/sshd && /usr/sbin/sshd -D & "
        "curl -fsSL https://tailscale.com/install.sh | sh && "
        "tailscaled --state=/var/lib/tailscale/tailscaled.state & "
        "sleep 3 && "
        f"tailscale up --hostname={container_name} > /tmp/ts.log 2>&1 & "
        "tail -f /dev/null"
    )

    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command=["sh", "-c", setup_script],
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        nano_cpus=nano_cpus,
        privileged=True,
        restart_policy={"Name": "unless-stopped"},
        volumes={
            os.path.join(data_dir, container_name): {"bind": "/data", "mode": "rw"}
        }
    )

    # 2. Extract Tailscale URL from log output
    login_url = None
    for _ in range(25):
        time.sleep(2)
        res = container.exec_run(["cat", "/tmp/ts.log"])
        output = res.output.decode("utf-8", errors="ignore") if res.output else ""
        
        match = re.search(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+", output)
        if match:
            login_url = match.group(0)
            break

    if not login_url:
        login_url = "https://login.tailscale.com"

    return container, login_url

# ---------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info("%s is online as %s", BOT_NAME, bot.user.name)

@bot.command(name="create")
@is_admin()
async def cmd_create(ctx, ram: str, cpu: int, disk: str, os_type: str, user: discord.Member):
    start_time = time.time()
    msg = await ctx.send(f"⏳ **Deploying VPS & Generating Tailscale Link** for {user.mention}...")
    
    try:
        ram_bytes = parse_size_to_bytes(ram)
        image_tag = get_normalized_os(os_type)
        container_name = f"vps-{user.id}-{int(time.time())}"
        root_password = generate_password()

        container, login_url = await asyncio.to_thread(
            _provision_vps_sync,
            image_tag, ram_bytes, cpu, container_name, root_password
        )

        vps_id = container.id[:10]
        db = load_db()
        db["vps"][vps_id] = {
            "container_id": container.id,
            "owner_id": user.id,
            "ram": ram,
            "cpu": cpu,
            "disk": disk,
            "os": os_type,
            "password": root_password,
            "login_url": login_url,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        save_db(db)

        elapsed = round(time.time() - start_time, 2)

        # DM Format matching the reference layout
        dm_embed = discord.Embed(
            title="🚀 VPS Deployed Instantly!",
            color=discord.Color.blue()
        )
        dm_embed.description = f"🔗 **Tailscale Login Link:**\n{login_url}"
        dm_embed.add_field(name="Instance ID", value=f"`{vps_id}`", inline=True)
        dm_embed.add_field(name="RAM / CPU", value=f"`{ram}` / `{cpu} Core(s)`", inline=True)
        dm_embed.add_field(
            name="🔑 SSH Credentials",
            value=f"**Port:** `22`\n**User:** `root`\n**Password:** `{root_password}`",
            inline=False
        )
        
        dm_sent = True
        try:
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            dm_sent = False

        status_msg = f"✅ **VPS Deployed in {elapsed}s!**\n**ID:** `{vps_id}`\n**Assigned To:** {user.mention}\n"
        status_msg += "📩 Credentials delivered to user's Direct Messages." if dm_sent else "⚠️ Could not DM user (DMs are closed)."

        await msg.edit(content=status_msg)

    except Exception as err:
        logging.exception("Deployment Error")
        await msg.edit(content=f"❌ **Deployment Failed:** `{str(err)}`")

if __name__ == "__main__":
    token = config.get("TOKEN")
    if not token or token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError("Specify valid bot TOKEN in config.json.")
    bot.run(token)
  
