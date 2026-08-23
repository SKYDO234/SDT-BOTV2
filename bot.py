import os
import sys
import json
import time
import re
import secrets
import string
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
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

# Load or initialize Configuration
if not os.path.exists(CONFIG_FILE):
    default_config = {
        "TOKEN": "YOUR_DISCORD_BOT_TOKEN_HERE",
        "PREFIX": "$",
        "ADMIN_IDS": [],
        "ANTINUKE_ENABLED": True,
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
DEFAULT_PAID_PLANS = {
    "1": {"id": 1, "name": "BASIC PLAN", "ram": "2g", "cpu": 1, "disk": "10g", "price": "1"},
    "2": {"id": 2, "name": "PRO PLAN", "ram": "5g", "cpu": 2, "disk": "20g", "price": "5"},
    "3": {"id": 3, "name": "HYPER PLAN", "ram": "8g", "cpu": 4, "disk": "30g", "price": "10"},
}

DEFAULT_INVITE_PLANS = {
    "4": {"id": 4, "name": "INVITE PLAN 1", "ram": "1g", "cpu": 2, "disk": "10g", "invites": 12},
    "5": {"id": 5, "name": "INVITE PLAN 2", "ram": "2g", "cpu": 1, "disk": "10g", "invites": 39},
    "6": {"id": 6, "name": "INVITE PLAN 3", "ram": "3g", "cpu": 1, "disk": "10g", "invites": 49},
}

def default_db():
    return {
        "vps": {},
        "admins": [int(x) for x in config.get("ADMIN_IDS", []) if str(x).isdigit()],
        "plans": DEFAULT_PAID_PLANS.copy(),
        "invite_plans": DEFAULT_INVITE_PLANS.copy(),
        "invites": {},
        "guild_invites": {},
        "member_inviter": {}
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
        raise ValueError("Invalid size. Use `m` or `g` (e.g. `1g`, `10g`).")
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
# FAST INSTANT PROVISIONING ENGINE
# ---------------------------------------------------------
def _fast_provision_sync(image_tag: str, ram_bytes: int, cpu_cores: int, container_name: str, root_password: str):
    if docker_client is None:
        raise RuntimeError("Docker daemon is offline.")

    data_dir = os.path.abspath(DEFAULT_DATA_DIR)
    os.makedirs(os.path.join(data_dir, container_name), exist_ok=True)
    nano_cpus = int(cpu_cores * 1_000_000_000)

    # Lightweight fast start command without apt updates
    startup_cmd = (
        f"echo 'root:{root_password}' | chpasswd && "
        "if [ -f /usr/sbin/sshd ]; then /usr/sbin/sshd -D; else tail -f /dev/null; fi"
    )

    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command=["sh", "-c", startup_cmd],
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        nano_cpus=nano_cpus,
        labels={"sdt-botv2": "vps"},
        restart_policy={"Name": "unless-stopped"},
        volumes={
            os.path.join(data_dir, container_name): {"bind": "/data", "mode": "rw"}
        },
        privileged=True
    )
    return container

# ---------------------------------------------------------
# EVENTS AND COMMANDS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info("%s is online as %s", BOT_NAME, bot.user.name)

@bot.command(name="plans")
async def cmd_plans(ctx):
    db = load_db()
    embed = discord.Embed(title="🛒 VPS PLANS", color=discord.Color.blurple())
    for key, p in sorted(db.get("plans", {}).items()):
        embed.add_field(
            name=f"Plan {p['id']}: {p['name']}",
            value=f"• **RAM:** `{p['ram']}`\n• **CPU:** `{p['cpu']}`\n• **Disk:** `{p['disk']}`\n• **Price:** `${p.get('price')}`",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="create")
@is_admin()
async def cmd_create(ctx, ram: str, cpu: int, disk: str, os_type: str, user: discord.Member):
    start_time = time.time()
    msg = await ctx.send(f"⚡ **Instantly Deploying VPS** for {user.mention}...")
    
    try:
        ram_bytes = parse_size_to_bytes(ram)
        image_tag = get_normalized_os(os_type)
        container_name = f"vps-{user.id}-{int(time.time())}"
        root_password = generate_password()

        # Run container creation directly without hanging background setups
        container = await asyncio.to_thread(
            _fast_provision_sync,
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
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        save_db(db)

        elapsed = round(time.time() - start_time, 2)

        # Send credentials via Direct Message
        dm_embed = discord.Embed(
            title="🚀 VPS Deployed Instantly!",
            color=discord.Color.green()
        )
        dm_embed.add_field(name="Instance ID", value=f"`{vps_id}`", inline=True)
        dm_embed.add_field(name="RAM / CPU", value=f"`{ram}` / `{cpu} Core(s)`", inline=True)
        dm_embed.add_field(name="SSH User", value="`root`", inline=True)
        dm_embed.add_field(name="Password", value=f"`{root_password}`", inline=False)
        
        try:
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        await msg.edit(
            content=f"✅ **VPS Deployed in {elapsed}s!**\n"
                    f"**ID:** `{vps_id}`\n"
                    f"**Assigned To:** {user.mention}\n"
                    f"📩 Credentials delivered to user's Direct Messages."
        )

    except Exception as err:
        logging.exception("Deployment Error")
        await msg.edit(content=f"❌ **Deployment Failed:** `{str(err)}`")

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    db = load_db()
    user_vps = [
        (vid, info) for vid, info in db.get("vps", {}).items()
        if int(info.get("owner_id", 0)) == ctx.author.id
    ]
    if not user_vps:
        await ctx.send("❌ **No active VPS instances found.**")
        return

    embed = discord.Embed(title="🖥️ Your Active VPS Instances", color=discord.Color.blue())
    for vid, info in user_vps:
        embed.add_field(
            name=f"ID: {vid}",
            value=f"**OS:** `{info['os']}` | **Specs:** `{info['ram']} RAM / {info['cpu']} CPU`\n**Password:** `{info['password']}`",
            inline=False
        )
    await ctx.send(embed=embed)

if __name__ == "__main__":
    token = config.get("TOKEN")
    if not token or token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError("Specify valid bot TOKEN in config.json.")
    bot.run(token)
