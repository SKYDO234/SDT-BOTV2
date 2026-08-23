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

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
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
# DATABASE / MIGRATION
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
        "antinuke": config.get("ANTINUKE_ENABLED", True),
        "plans": DEFAULT_PAID_PLANS.copy(),
        "invite_plans": DEFAULT_INVITE_PLANS.copy(),
        "invites": {},
        "guild_invites": {},
        "invite_claims": {},
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
    except (json.JSONDecodeError, OSError):
        logging.warning("Database was unreadable; rebuilding a safe database.")
        data = default_db()

    defaults = default_db()
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
    if not isinstance(data.get("vps"), dict):
        data["vps"] = {}
    if not isinstance(data.get("admins"), list):
        data["admins"] = defaults["admins"]
    if not isinstance(data.get("invites"), dict):
        data["invites"] = {}
    if not isinstance(data.get("guild_invites"), dict):
        data["guild_invites"] = {}
    if not isinstance(data.get("invite_claims"), dict):
        data["invite_claims"] = {}
    if not isinstance(data.get("member_inviter"), dict):
        data["member_inviter"] = {}

    for k, v in DEFAULT_PAID_PLANS.items():
        data["plans"].setdefault(k, v)
    for k, v in DEFAULT_INVITE_PLANS.items():
        data["invite_plans"].setdefault(k, v)

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
bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None
)

invite_cache = {}

def generate_password(length=18):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_size_to_bytes(size_str: str) -> int:
    size_str = str(size_str).lower().strip()
    match = re.fullmatch(r"(\d+)([mg])", size_str)
    if not match:
        raise ValueError("Invalid size. Use `m` or `g`, e.g. `512m`, `1g`, `10g`.")
    num, unit = match.groups()
    num = int(num)
    bytes_val = num * (1024 ** 2 if unit == "m" else 1024 ** 3)
    if bytes_val < 256 * 1024 * 1024:
        raise ValueError("Memory/disk allocation must be at least 256m.")
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
    raise ValueError(
        f"Unsupported OS version: `{os_input}`. Supported: Ubuntu 20.04/22.04, Debian 10/11/12/13."
    )

def active_vps_for_user(db, user_id):
    return [
        (vps_id, info)
        for vps_id, info in db.get("vps", {}).items()
        if int(info.get("owner_id", 0)) == int(user_id)
        and info.get("status") not in {"DELETED", "ERROR"}
    ]

def safe_int(value, field):
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"`{field}` must be a number.")
    if n <= 0:
        raise ValueError(f"`{field}` must be greater than 0.")
    return n

# ---------------------------------------------------------
# INVITE TRACKING
# ---------------------------------------------------------
def invite_stats(db, guild_id, user_id):
    guild_data = db.setdefault("invites", {}).setdefault(str(guild_id), {})
    return guild_data.setdefault(
        str(user_id),
        {"joins": 0, "leaves": 0, "fake": 0, "uses": 0}
    )

async def refresh_guild_invites(guild):
    if not guild.me or not guild.me.guild_permissions.manage_guild:
        return
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        db = load_db()
        db.setdefault("guild_invites", {})[str(guild.id)] = invite_cache[guild.id]
        save_db(db)
    except Exception:
        pass

async def detect_used_invite(guild):
    try:
        current = await guild.invites()
    except Exception:
        return None

    before = invite_cache.get(guild.id, {})
    used = None
    for inv in current:
        old_uses = before.get(inv.code, 0)
        if inv.uses > old_uses:
            used = inv
            break

    invite_cache[guild.id] = {inv.code: inv.uses for inv in current}
    return used

# ---------------------------------------------------------
# NON-BLOCKING DOCKER PROVISIONING ENGINE
# ---------------------------------------------------------
async def ensure_docker_image(image_tag: str):
    """Ensures Docker images are pulled using async subprocesses to prevent blocking."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", image_tag,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    if await proc.wait() != 0:
        logging.info(f"Pulling required image non-blockingly: {image_tag}")
        pull_proc = await asyncio.create_subprocess_exec("docker", "pull", image_tag)
        await pull_proc.wait()

def _provision_vps_sync(
    image_tag: str,
    ram_bytes: int,
    cpu_cores: int,
    disk_bytes: int,
    container_name: str,
    root_password: str
):
    if docker_client is None:
        raise RuntimeError("Docker daemon is not available.")

    data_dir = os.path.abspath(DEFAULT_DATA_DIR)
    os.makedirs(os.path.join(data_dir, container_name), exist_ok=True)
    nano_cpus = int(cpu_cores * 1_000_000_000)

    host_cpus = os.cpu_count() or 1
    used = 0
    try:
        for c in docker_client.containers.list(all=True, filters={"label": "sdt-botv2=vps"}):
            labels = c.labels
            if labels.get("sdt-cpu"):
                used += int(labels["sdt-cpu"])
    except Exception:
        used = 0

    cpus = []
    if cpu_cores <= host_cpus:
        start = used % host_cpus
        cpus = [(start + i) % host_cpus for i in range(cpu_cores)]
    cpuset = ",".join(map(str, sorted(set(cpus)))) if cpus else None

    root_cmd = (
        "set -e; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y --no-install-recommends openssh-server ca-certificates curl neofetch; "
        "mkdir -p /run/sshd; "
        "echo 'root:{password}' | chpasswd; "
        "printf '%s\\n' 'PermitRootLogin yes' >> /etc/ssh/sshd_config; "
        "printf '%s\\n' 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; "
        "printf '%s\\n' 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config; "
        "printf '%s\\n' 'UsePAM no' >> /etc/ssh/sshd_config; "
        "sshd -t; "
        "exec /usr/sbin/sshd -D -e"
    ).format(password=root_password.replace("'", "'\"'\"'"))

    container_kwargs = dict(
        image=image_tag,
        name=container_name,
        command=["bash", "-lc", root_cmd],
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        memswap_limit=ram_bytes,
        nano_cpus=nano_cpus,
        labels={
            "sdt-botv2": "vps",
            "sdt-cpu": str(cpu_cores),
            "sdt-owner-container": container_name
        },
        restart_policy={"Name": "unless-stopped"},
        environment={
            "SDT_ALLOC_RAM": str(ram_bytes // (1024 ** 3)) + "GB",
            "SDT_ALLOC_CPU": str(cpu_cores),
            "SDT_ALLOC_DISK": str(disk_bytes // (1024 ** 3)) + "GB",
            "SDT_ALLOC_OS": image_tag
        },
        volumes={
            os.path.join(data_dir, container_name): {
                "bind": "/data",
                "mode": "rw"
            }
        },
        privileged=True
    )

    if cpuset:
        container_kwargs["cpuset_cpus"] = cpuset

    container = None
    ts_container = None

    try:
        container = docker_client.containers.run(**container_kwargs)

        ts_name = f"ts-{container_name}"
        state_dir = os.path.join(data_dir, f"{container_name}-tailscale")
        os.makedirs(state_dir, exist_ok=True)

        ts_container = docker_client.containers.run(
            image="tailscale/tailscale:latest",
            name=ts_name,
            command=[
                "tailscaled",
                "--state=/var/lib/tailscale/tailscaled.state"
            ],
            network_mode=f"container:{container.id}",
            detach=True,
            privileged=True,
            cap_add=["NET_ADMIN", "NET_RAW"],
            devices=["/dev/net/tun:/dev/net/tun"],
            labels={"sdt-botv2": "tailscale"},
            volumes={
                state_dir: {
                    "bind": "/var/lib/tailscale",
                    "mode": "rw"
                }
            },
            restart_policy={"Name": "unless-stopped"}
        )

        auth_url = None
        for _ in range(15):
            result = ts_container.exec_run(
                ["tailscale", "up", "--qr=false", "--hostname", container_name],
                demux=False
            )
            output = result.output.decode("utf-8", errors="ignore") if result.output else ""
            match = re.search(r"https://login\.tailscale\.com/[^\s]+", output)
            if match:
                auth_url = match.group(0).rstrip(").,")
                break
            time.sleep(1.5)

        if not auth_url:
            logs = ts_container.logs(tail=100).decode("utf-8", errors="ignore")
            match = re.search(r"https://login\.tailscale\.com/[^\s]+", logs)
            if match:
                auth_url = match.group(0).rstrip(").,")

        if not auth_url:
            raise RuntimeError("Tailscale did not generate a login URL. Verify system network configuration.")

        tailscale_ipv4 = None
        for _ in range(5):
            result = ts_container.exec_run(["tailscale", "ip", "-4"])
            out = result.output.decode("utf-8", errors="ignore").strip() if result.output else ""
            m = re.search(r"\b100\.(?:\d{1,3}\.){2}\d{1,3}\b", out)
            if m:
                tailscale_ipv4 = m.group(0)
                break
            time.sleep(1)

        return container, ts_container, auth_url, tailscale_ipv4

    except Exception:
        if ts_container is not None:
            try:
                ts_container.remove(force=True)
            except Exception:
                pass
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        raise

async def provision_vps(image_tag, ram_bytes, cpu_cores, disk_bytes, container_name, root_password):
    await ensure_docker_image(image_tag)
    await ensure_docker_image("tailscale/tailscale:latest")
    
    return await asyncio.to_thread(
        _provision_vps_sync,
        image_tag, ram_bytes, cpu_cores, disk_bytes, container_name, root_password
    )

# ---------------------------------------------------------
# PERSISTENCE & REPAIR
# ---------------------------------------------------------
def record_vps(db, vps_id, container, owner, ram, cpu, disk, os_key, password, plan_id=None, invite_reward=False):
    db["vps"][vps_id] = {
        "container_id": container.id,
        "container_name": container.name,
        "owner_id": owner.id,
        "owner_tag": str(owner),
        "ram": ram,
        "cpu": cpu,
        "disk": disk,
        "os": os_key,
        "password": password,
        "status": "ACTIVE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "invite_reward": invite_reward
    }

def get_vps_status(info):
    if docker_client is None:
        return "DOCKER OFFLINE"
    try:
        c = docker_client.containers.get(info["container_id"])
        return "ACTIVE" if c.status == "running" else c.status.upper()
    except docker.errors.NotFound:
        return "MISSING"
    except Exception:
        return "UNKNOWN"

# ---------------------------------------------------------
# DISCORD EVENTS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info("%s online as %s (%s)", BOT_NAME, bot.user.name, bot.user.id)
    for guild in bot.guilds:
        await refresh_guild_invites(guild)
    if not invite_refresh_loop.is_running():
        invite_refresh_loop.start()
    if not vps_health_loop.is_running():
        vps_health_loop.start()

@bot.event
async def on_guild_join(guild):
    await refresh_guild_invites(guild)

@bot.event
async def on_member_join(member):
    if member.bot:
        return
    invite = await detect_used_invite(member.guild)
    if invite is None or invite.inviter is None:
        return

    inviter_id = invite.inviter.id
    db = load_db()
    stats = invite_stats(db, member.guild.id, inviter_id)
    stats["joins"] += 1
    stats["uses"] += 1

    account_age = datetime.now(timezone.utc) - member.created_at
    if account_age < timedelta(days=7):
        stats["fake"] += 1

    db.setdefault("member_inviter", {})[f"{member.guild.id}:{member.id}"] = inviter_id
    save_db(db)

@bot.event
async def on_member_remove(member):
    db = load_db()
    key = f"{member.guild.id}:{member.id}"
    inviter_id = db.get("member_inviter", {}).pop(key, None)
    if inviter_id is not None:
        stats = invite_stats(db, member.guild.id, inviter_id)
        stats["leaves"] += 1
    save_db(db)

@tasks.loop(minutes=10)
async def invite_refresh_loop():
    for guild in bot.guilds:
        await refresh_guild_invites(guild)

@invite_refresh_loop.before_loop
async def before_invite_refresh_loop():
    await bot.wait_until_ready()

@tasks.loop(minutes=2)
async def vps_health_loop():
    db = load_db()
    changed = False
    for vps_id, info in db.get("vps", {}).items():
        try:
            container = docker_client.containers.get(info["container_id"])
            info["status"] = "ACTIVE" if container.status == "running" else container.status.upper()
            ts_id = info.get("tailscale_container_id")
            if ts_id:
                ts = docker_client.containers.get(ts_id)
                if ts.status == "running":
                    result = ts.exec_run(["tailscale", "ip", "-4"])
                    out = result.output.decode("utf-8", errors="ignore") if result.output else ""
                    match = re.search(r"\b100\.(?:\d{1,3}\.){2}\d{1,3}\b", out)
                    if match:
                        info["tailscale_ipv4"] = match.group(0)
            changed = True
        except Exception:
            if info.get("status") != "MISSING":
                info["status"] = "MISSING"
                changed = True
    if changed:
        save_db(db)

@vps_health_loop.before_loop
async def before_vps_health_loop():
    await bot.wait_until_ready()

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------
@bot.command(name="about")
async def cmd_about(ctx):
    embed = discord.Embed(
        title="🚀 SDT-BOTV2 • ABOUT",
        description=(
            "🚀 **IT IS A VERY GOOD HOSTING SERVER YOU WILL GET MANY PLANS OF**\n\n"
            "VPS\n"
            "MINECRAFT SERVER HOSTING\n"
            "BOT HOSTING\n\n"
            "**DEVELOPED BY SKYDOXD**"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text=BOT_NAME)
    await ctx.send(embed=embed)

@bot.command(name="plans")
async def cmd_plans(ctx):
    db = load_db()
    embed = discord.Embed(
        title="🛒 VPS PLANS",
        description="Choose a plan and create a ticket to buy.",
        color=discord.Color.blurple()
    )
    for key in sorted(db.get("plans", {}), key=lambda x: int(x)):
        p = db["plans"][key]
        embed.add_field(
            name=f"Plan {p['id']}: {p['name']}",
            value=(
                f"• **RAM:** `{p['ram']}`\n"
                f"• **CPU:** `{p['cpu']}`\n"
                f"• **Disk:** `{p['disk']}`\n"
                f"• **Price:** `${p.get('price', 'N/A')}`"
            ),
            inline=False
        )
    embed.set_footer(text="🎫 Create a ticket to buy a VPS.")
    await ctx.send(embed=embed)

@bot.command(name="invite")
async def cmd_invite(ctx):
    db = load_db()
    stats = invite_stats(db, ctx.guild.id, ctx.author.id)
    valid = max(0, stats["joins"] - stats["fake"])
    total = stats["joins"]
    leaves = stats["leaves"]
    fake = stats["fake"]
    embed = discord.Embed(
        title=f"📨 {ctx.author.display_name}'s Invite Stat",
        description="🤖 **SDT-BOTV2 monitors the server's invite counters 24/7 while online.**",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.add_field(name="👥 Total Joins", value=f"`{total}`", inline=True)
    embed.add_field(name="✅ Valid", value=f"`{valid}`", inline=True)
    embed.add_field(name="🚪 Leaves", value=f"`{leaves}`", inline=True)
    embed.add_field(name="⚠️ Fake", value=f"`{fake}`", inline=True)
    embed.add_field(name="🎯 Current Invites", value=f"`{valid}`", inline=True)
    embed.add_field(name="🆔 User ID", value=f"`{ctx.author.id}`", inline=True)
    await ctx.send(embed=embed)

class InvitePlanView(discord.ui.View):
    def __init__(self, owner_id, plans):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        options = []
        for key in sorted(plans, key=lambda x: int(x)):
            p = plans[key]
            options.append(
                discord.SelectOption(
                    label=str(p["name"])[:100],
                    value=str(p["id"]),
                    description=(
                        f"{p['ram']} RAM • {p['cpu']} CPU • {p['disk']} Disk • "
                        f"{p.get('invites', 0)} invites"
                    )[:100]
                )
            )
        select = discord.ui.Select(
            placeholder="Choose your invite-reward VPS plan",
            options=options[:25]
        )
        select.callback = self.choose
        self.add_item(select)

    async def choose(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This menu belongs to another user.", ephemeral=True)
            return

        select = interaction.data.get("values", []) if interaction.data else []
        if not select:
            await interaction.response.send_message("❌ No plan selected.", ephemeral=True)
            return

        db = load_db()
        plan = db.get("invite_plans", {}).get(str(select[0]))
        if not plan:
            await interaction.response.send_message("❌ This plan no longer exists.", ephemeral=True)
            return

        user_id = interaction.user.id
        existing = [
            info for _, info in active_vps_for_user(db, user_id)
            if info.get("invite_reward")
        ]
        if existing:
            await interaction.response.send_message(
                "❌ You already have an invite-reward VPS. Only an admin can give you another one.",
                ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            return

        stats = invite_stats(db, interaction.guild.id, user_id)
        current_invites = max(0, stats["joins"] - stats["fake"] - stats["leaves"])
        required = int(plan.get("invites", 0))

        if required <= 0:
            await interaction.response.send_message(
                "❌ This invite-reward plan has no valid invite requirement. Ask an admin to customize it first.",
                ephemeral=True
            )
            return

        if current_invites < required:
            await interaction.response.send_message(
                f"❌ You need at least **{required} valid invites** for **{plan['name']}**. "
                f"You currently have **{current_invites}**.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            member = interaction.guild.get_member(user_id)
            if member is None:
                member = await interaction.guild.fetch_member(user_id)

            result = await create_vps_internal(
                owner=member,
                ram=plan["ram"],
                cpu=int(plan["cpu"]),
                disk=plan["disk"],
                os_type="Ubuntu22.04",
                plan_id=int(plan["id"]),
                invite_reward=True
            )

            db = load_db()
            db.setdefault("invite_claims", {})[str(user_id)] = {
                "plan_id": int(plan["id"]),
                "vps_id": result["vps_id"],
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            save_db(db)

            await interaction.followup.send(
                f"✅ **VPS created successfully for {member.mention}.**\n"
                f"**Plan:** `{plan['name']}`\n"
                f"**Hardware:** `{plan['ram']} RAM • {plan['cpu']} CPU • {plan['disk']} Disk`\n"
                f"📩 Check your DMs for the Tailscale login and SSH credentials.",
                ephemeral=True
            )
        except Exception as exc:
            logging.exception("Invite reward provisioning failed")
            await interaction.followup.send(
                f"❌ **Deployment failed:** `{str(exc)[:900]}`",
                ephemeral=True
            )

@bot.command(name="invite-rewards")
async def cmd_invite_rewards(ctx):
    db = load_db()
    embed = discord.Embed(
        title="🎁 VPS INVITE REWARDS",
        description="Invite people, reach a plan's requirement, then choose your VPS.",
        color=discord.Color.gold()
    )
    for key in sorted(db.get("invite_plans", {}), key=lambda x: int(x)):
        p = db["invite_plans"][key]
        embed.add_field(
            name=f"Plan {p['id']}: {p['name']}",
            value=(
                f"• **RAM:** `{p['ram']}`\n"
                f"• **CPU:** `{p['cpu']}`\n"
                f"• **Disk:** `{p['disk']}`\n"
                f"• **Invites required:** `{p.get('invites', 0)}`"
            ),
            inline=False
        )
    embed.set_footer(text="Select a plan below. Ubuntu22.04 is used automatically.")
    await ctx.send(embed=embed, view=InvitePlanView(ctx.author.id, db.get("invite_plans", {})))

@bot.command(name="customize-plans")
@is_admin()
async def cmd_customize_plans(ctx, plan_id: int, name: str, ram: str, cpu: int, disk: str, prize: str):
    try:
        safe_int(plan_id, "id")
        safe_int(cpu, "cpu")
        parse_size_to_bytes(ram)
        parse_size_to_bytes(disk)
        if plan_id in (4, 5, 6):
            required = safe_int(prize, "invites")
        else:
            if not re.fullmatch(r"\d+(?:\.\d{1,2})?", prize):
                raise ValueError("Prize must be a number such as `1` or `5.99`.")
            required = prize

        db = load_db()
        if plan_id in (1, 2, 3):
            db["plans"][str(plan_id)] = {
                "id": plan_id, "name": name, "ram": ram.lower(),
                "cpu": int(cpu), "disk": disk.lower(), "price": str(required)
            }
            kind = "paid"
        elif plan_id in (4, 5, 6):
            db["invite_plans"][str(plan_id)] = {
                "id": plan_id, "name": name, "ram": ram.lower(),
                "cpu": int(cpu), "disk": disk.lower(), "invites": int(required)
            }
            kind = "invite-reward"
        else:
            await ctx.send("❌ Valid plan IDs are `1-6`.")
            return

        save_db(db)
        await ctx.send(
            f"✅ **{kind.title()} plan updated.**\n"
            f"**ID:** `{plan_id}`\n**Name:** `{name}`\n"
            f"**RAM:** `{ram}` • **CPU:** `{cpu}` • **Disk:** `{disk}` • "
            f"**{'Invites' if plan_id >= 4 else 'Price'}:** `{required}`"
        )
    except ValueError as exc:
        await ctx.send(f"❌ **Parameter Error:** {exc}")

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    db = load_db()
    user_vps = active_vps_for_user(db, ctx.author.id)
    if not user_vps:
        await ctx.send("❌ **No active VPS instances found.**")
        return

    embed = discord.Embed(title="🖥️ Your Managed VPS Instances", color=discord.Color.green())
    for vps_id, info in user_vps:
        status = get_vps_status(info)
        embed.add_field(
            name=f"Instance ID: {vps_id}",
            value=(
                f"**OS:** `{info['os']}` | **Status:** `{status}`\n"
                f"**CPU:** `{info['cpu']} Core(s)` | **RAM:** `{info['ram']}` | **Disk:** `{info['disk']}`\n"
                f"**SSH User:** `root` | **Password:** `{info.get('password', 'N/A')}`"
            ),
            inline=False
        )
    await ctx.send(embed=embed)

async def create_vps_internal(owner, ram, cpu, disk, os_type, plan_id=None, invite_reward=False):
    ram_bytes = parse_size_to_bytes(ram)
    disk_bytes = parse_size_to_bytes(disk)
    cpu = safe_int(cpu, "cpu")
    os_key, image_tag = get_normalized_os(os_type)

    container_name = f"vps-{owner.id}-{int(time.time())}"
    root_password = generate_password()

    # Timeout extended to 600 seconds to account for initial cloud image pulling
    container, ts_container, login_url, ts_ipv4 = await asyncio.wait_for(
        provision_vps(
            image_tag,
            ram_bytes,
            cpu,
            disk_bytes,
            container_name,
            root_password
        ),
        timeout=600.0
    )

    vps_id = container.id[:10]
    db = load_db()
    record_vps(
        db, vps_id, container, owner, ram, cpu, disk, os_key,
        root_password, plan_id=plan_id, invite_reward=invite_reward
    )
    db["vps"][vps_id]["tailscale_container_id"] = ts_container.id
    db["vps"][vps_id]["tailscale_ipv4"] = ts_ipv4
    save_db(db)

    dm_embed = discord.Embed(
        title="🚀 Your VPS is Ready!",
        description=(
            "Authorize the Tailscale node using the login link. "
            "After authorization, use the **Tailscale IPv4** as the SSH host."
        ),
        color=discord.Color.blue()
    )
    dm_embed.add_field(name="🤖 Bot", value=f"`{BOT_NAME}`", inline=True)
    dm_embed.add_field(name="Instance ID", value=f"`{vps_id}`", inline=True)
    dm_embed.add_field(name="Allocated RAM", value=f"`{ram}`", inline=True)
    dm_embed.add_field(name="Allocated vCPU", value=f"`{cpu} Core(s)`", inline=True)
    dm_embed.add_field(name="Disk Quota", value=f"`{disk}`", inline=True)
    dm_embed.add_field(name="OS Distribution", value=f"`{os_key}`", inline=True)
    dm_embed.add_field(name="🔑 Tailscale Login", value=login_url, inline=False)
    dm_embed.add_field(
        name="🌐 Tailscale IPv4",
        value=f"`{ts_ipv4 or 'Authorize node first, then run $myvps'}`",
        inline=False
    )
    dm_embed.add_field(
        name="🔐 SSH",
        value=f"**Port:** `22`\n**User:** `root`\n**Password:** `{root_password}`",
        inline=False
    )

    try:
        await owner.send(embed=dm_embed)
    except discord.Forbidden:
        logging.warning("Could not DM credentials to %s", owner.id)

    return {
        "vps_id": vps_id,
        "login_url": login_url,
        "tailscale_ipv4": ts_ipv4,
        "password": root_password
    }

@bot.command(name="create")
@is_admin()
async def cmd_create(ctx, ram: str, cpu: int, disk: str, os_type: str, user: discord.Member):
    try:
        await ctx.send(
            f"⏳ **Provisioning VPS** for {user.mention} with exactly "
            f"`{ram} RAM • {cpu} CPU • {disk} disk`..."
        )
        result = await create_vps_internal(
            owner=user,
            ram=ram,
            cpu=cpu,
            disk=disk,
            os_type=os_type,
            invite_reward=False
        )
        await ctx.send(
            f"✅ **VPS Provisioned Successfully!**\n"
            f"**ID:** `{result['vps_id']}`\n"
            f"**Assigned To:** {user.mention}\n"
            f"📩 Credentials sent via DM."
        )
    except Exception as err:
        logging.exception("Error provisioning VPS")
        await ctx.send(f"❌ **Deployment Failed:** `{str(err)[:1500]}`")

@bot.command(name="vps-status")
@is_admin()
async def cmd_vps_status(ctx):
    db = load_db()
    lines = []
    for vps_id, info in db.get("vps", {}).items():
        status = get_vps_status(info)
        lines.append(
            f"`{vps_id}` • <@{info.get('owner_id')}> • "
            f"{info.get('ram')} RAM / {info.get('cpu')} CPU / {info.get('disk')} Disk • `{status}`"
        )
    if not lines:
        await ctx.send("ℹ️ No VPS records found.")
        return
    await ctx.send("🖥️ **VPS STATUS**\n" + "\n".join(lines[:50]))

# ---------------------------------------------------------
# ERROR HANDLING
# ---------------------------------------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument. Check command syntax.")
        return
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.CommandInvokeError):
        logging.exception("Command error", exc_info=error.original)
        await ctx.send(f"❌ Command failed: `{str(error.original)[:1000]}`")
        return
    logging.exception("Unhandled command error", exc_info=error)
    await ctx.send("❌ An unexpected error occurred.")

# ---------------------------------------------------------
# BOT ENTRY POINT
# ---------------------------------------------------------
if __name__ == "__main__":
    token = config.get("TOKEN")
    if not token or token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError("Set your real Discord bot token in config.json before running.")
    bot.run(token)
