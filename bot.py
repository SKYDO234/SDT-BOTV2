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
        "member_inviter": {},
        "antinuke": config.get("ANTINUKE_ENABLED", True)
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
    match = re.match(r"^(\d+)([mg])$", size_str)
    if not match:
        raise ValueError("Invalid format. Use numbers followed by 'm' or 'g' (e.g., 512m, 10g).")
    num, unit = match.groups()
    num = int(num)
    bytes_val = num * 1024 * 1024 if unit == "m" else num * 1024 * 1024 * 1024
    if bytes_val < 256 * 1024 * 1024:
        raise ValueError("Memory allocation too small. Specify at least 256m.")
    return bytes_val

def is_admin():
    async def predicate(ctx):
        db = load_db()
        admins = {int(x) for x in db.get("admins", []) if str(x).isdigit()}
        if ctx.author.id in admins or ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("❌ **Access Denied:** You do not have permission to execute this command.")
        return False
    return commands.check(predicate)

def get_normalized_os(os_input: str) -> tuple:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", os_input).upper()
    cleaned = cleaned.replace("DEBAIN", "DEBIAN")
    
    mapping = {
        "UBUNTU2204": "ubuntu:22.04",
        "UBUNTU2004": "ubuntu:20.04",
        "DEBIAN10": "debian:10",
        "DEBIAN11": "debian:11",
        "DEBIAN12": "debian:12",
        "DEBIAN13": "debian:13"
    }
    
    if cleaned in mapping:
        return cleaned, mapping[cleaned]
    raise ValueError(f"Unsupported OS version: `{os_input}`. Supported: Ubuntu 20.04/22.04, Debian 10/11/12/13.")

# ---------------------------------------------------------
# PROVISIONING ENGINE (EXACT SOURCE PROVISIONING)
# ---------------------------------------------------------
def _provision_vps_sync(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    if docker_client is None:
        raise RuntimeError("Docker daemon is offline.")

    data_dir = os.path.abspath(config.get("DEFAULT_DATA_DIR", "./vps_data"))
    os.makedirs(f"{data_dir}/{container_name}", exist_ok=True)
    
    nano_cpus = int(cpu_cores * 1_000_000_000)

    # 1. Pull OS Base Image
    try:
        docker_client.images.get(image_tag)
    except docker.errors.ImageNotFound:
        logging.info(f"Image {image_tag} not found locally. Pulling from Docker Hub...")
        docker_client.images.pull(image_tag)

    # 2. Pull Tailscale Image
    try:
        docker_client.images.get("tailscale/tailscale:latest")
    except docker.errors.ImageNotFound:
        logging.info("Tailscale image not found locally. Pulling from Docker Hub...")
        docker_client.images.pull("tailscale/tailscale:latest")

    # 3. Launch Main OS Container with SSH
    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command="bash -c 'apt-get update && apt-get install -y openssh-server && mkdir -p /var/run/sshd && echo \"root:" + root_password + "\" | chpasswd && sed -i \"s/#PermitRootLogin.*/PermitRootLogin yes/g\" /etc/ssh/sshd_config && /usr/sbin/sshd -D'",
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        nano_cpus=nano_cpus,
        volumes={f"{data_dir}/{container_name}": {"bind": "/data", "mode": "rw"}},
        privileged=True
    )

    # 4. Launch Tailscale Container with Shared Network
    ts_container_name = f"ts-{container_name}"
    clean_hostname = container_name.replace("_", "-")
    
    ts_container = docker_client.containers.run(
        image="tailscale/tailscale:latest",
        name=ts_container_name,
        environment={
            "TS_HOSTNAME": clean_hostname,
            "TS_USERSPACE": "true"
        },
        network_mode=f"container:{container.id}",
        detach=True,
        privileged=True
    )

    time.sleep(3)

    # 5. Extract Tailscale Authentication URL
    auth_url = None
    for _ in range(10):
        try:
            exec_res = ts_container.exec_run("tailscale up --qr=false")
            output = exec_res.output.decode("utf-8", errors="ignore")
            
            match = re.search(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+", output)
            if match:
                auth_url = match.group(0)
                break

            logs = ts_container.logs().decode("utf-8", errors="ignore")
            match_logs = re.search(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+", logs)
            if match_logs:
                auth_url = match_logs.group(0)
                break
        except Exception as e:
            logging.warning(f"Tailscale link fetch attempt error: {e}")
            
        time.sleep(2)

    if not auth_url:
        container.stop()
        container.remove()
        ts_container.stop()
        ts_container.remove()
        raise RuntimeError("Failed to capture Tailscale Auth URL. Please verify container network access.")

    return container, ts_container, auth_url

async def provision_vps(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    return await asyncio.to_thread(_provision_vps_sync, image_tag, ram_bytes, cpu_cores, disk_bytes, container_name, root_password)

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user.name} ({bot.user.id})")

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

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    db = load_db()
    user_vps = [(vps_id, info) for vps_id, info in db.get("vps", {}).items() if info.get("owner_id") == ctx.author.id]

    if not user_vps:
        await ctx.send("❌ **No active VPS instances found.**")
        return

    embed = discord.Embed(title="🖥️ Your Managed VPS Instances", color=discord.Color.green())
    for vps_id, info in user_vps:
        embed.add_field(
            name=f"Instance ID: {vps_id}",
            value=(
                f"**OS:** `{info['os']}` | **Status:** `{info.get('status', 'ACTIVE')}`\n"
                f"**CPU:** `{info['cpu']} Core(s)` | **RAM:** `{info['ram']}` | **Disk:** `{info['disk']}`\n"
                f"**SSH User:** `root` | **Password:** `{info.get('password', 'N/A')}`\n"
                f"**Login Link:** {info.get('login_url', 'N/A')}"
            ),
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="create")
@is_admin()
async def cmd_create(ctx, ram: str, cpu: int, disk: str, os_type: str, user: discord.Member):
    """Syntax: $create <ram> <cpu> <disk> <os> <user>"""
    try:
        ram_bytes = parse_size_to_bytes(ram)
        disk_bytes = parse_size_to_bytes(disk)
        os_key, image_tag = get_normalized_os(os_type)
    except ValueError as e:
        await ctx.send(f"❌ **Parameter Error:** {e}")
        return

    status_msg = await ctx.send(f"⏳ **[1/2]** Provisioning VPS ({ram} RAM, {cpu} Core(s)) for {user.mention}...")
    
    container_name = f"vps-{user.id}-{int(time.time())}"
    root_password = generate_password()

    try:
        container, ts_container, login_url = await asyncio.wait_for(
            provision_vps(image_tag, ram_bytes, cpu, disk_bytes, container_name, root_password),
            timeout=120.0
        )

        await status_msg.edit(content=f"⏳ **[2/2]** Delivering Tailscale access link to {user.mention} via DM...")

        vps_id = container.id[:10]

        dm_embed = discord.Embed(
            title="🚀 Your VPS is Ready!",
            description="Click the link below to authorize this instance and attach it to your Tailscale network.",
            color=discord.Color.blue()
        )
        dm_embed.add_field(name="Instance ID", value=f"`{vps_id}`", inline=True)
        dm_embed.add_field(name="Allocated RAM", value=f"`{ram}`", inline=True)
        dm_embed.add_field(name="Allocated vCPU", value=f"`{cpu} Core(s)`", inline=True)
        dm_embed.add_field(name="Disk Storage", value=f"`{disk}`", inline=True)
        dm_embed.add_field(name="OS Distribution", value=f"`{os_key}`", inline=True)
        dm_embed.add_field(name="🔑 Tailscale Login Link", value=f"{login_url}", inline=False)
        dm_embed.add_field(name="🔐 SSH Credentials", value=f"**Port:** `22`\n**User:** `root`\n**Password:** `{root_password}`", inline=False)

        try:
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            container.stop()
            container.remove()
            ts_container.stop()
            ts_container.remove()
            await status_msg.edit(content=f"❌ **Deployment Aborted:** Could not DM {user.mention}. Direct Messages must be enabled.")
            return

        db = load_db()
        db["vps"][vps_id] = {
            "container_id": container.id,
            "ts_container_id": ts_container.id,
            "container_name": container_name,
            "owner_id": user.id,
            "owner_tag": str(user),
            "ram": ram,
            "cpu": cpu,
            "disk": disk,
            "os": os_key,
            "password": root_password,
            "login_url": login_url,
            "status": "ACTIVE",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        save_db(db)

        await status_msg.edit(content=f"✅ **VPS Provisioned Successfully!**\n**ID:** `{vps_id}`\n**Assigned To:** {user.mention}\n📩 **Login link delivered to Direct Messages.**")

    except Exception as err:
        logging.error(f"Error provisioning VPS: {err}", exc_info=True)
        await status_msg.edit(content=f"❌ **Deployment Failed:** `{err}`")

if __name__ == "__main__":
    bot.run(config.get("TOKEN"))
  
