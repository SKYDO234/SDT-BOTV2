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
from discord.ui import Select, View
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
# PROVISIONING ENGINE WITH TAILSCALE SSH FIX
# ---------------------------------------------------------
def _provision_vps_sync(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    if docker_client is None:
        raise RuntimeError("Docker daemon is offline.")

    data_dir = os.path.abspath(config.get("DEFAULT_DATA_DIR", "./vps_data"))
    os.makedirs(f"{data_dir}/{container_name}", exist_ok=True)
    
    nano_cpus = int(cpu_cores * 1_000_000_000)

    try:
        docker_client.images.get(image_tag)
    except docker.errors.ImageNotFound:
        docker_client.images.pull(image_tag)

    try:
        docker_client.images.get("tailscale/tailscale:latest")
    except docker.errors.ImageNotFound:
        docker_client.images.pull("tailscale/tailscale:latest")

    # Launch Primary Container with SSH and Root Authentication
    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command="bash -c 'apt-get update && apt-get install -y openssh-server iptables && mkdir -p /var/run/sshd && echo \"root:" + root_password + "\" | chpasswd && sed -i \"s/#PermitRootLogin.*/PermitRootLogin yes/g\" /etc/ssh/sshd_config && /usr/sbin/sshd -D'",
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        nano_cpus=nano_cpus,
        volumes={f"{data_dir}/{container_name}": {"bind": "/data", "mode": "rw"}},
        privileged=True
    )

    ts_container_name = f"ts-{container_name}"
    clean_hostname = container_name.replace("_", "-")
    
    # Launch Sidecar with TS_SSH Enabled to Allow Termux/Termius Port Routing
    ts_container = docker_client.containers.run(
        image="tailscale/tailscale:latest",
        name=ts_container_name,
        environment={
            "TS_HOSTNAME": clean_hostname,
            "TS_USERSPACE": "false",
            "TS_EXTRA_ARGS": "--ssh"
        },
        network_mode=f"container:{container.id}",
        detach=True,
        privileged=True
    )

    time.sleep(3)

    auth_url = None
    for _ in range(12):
        try:
            exec_res = ts_container.exec_run("tailscale up --ssh --qr=false")
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
# INVITE TRACKER ENGINE
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user.name} ({bot.user.id})")
    db = load_db()
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            db["guild_invites"][str(guild.id)] = {inv.code: inv.uses for inv in invites}
        except Exception as e:
            logging.error(f"Error fetching invites for {guild.name}: {e}")
    save_db(db)

@bot.event
async def on_member_join(member):
    db = load_db()
    guild = member.guild
    guild_id = str(guild.id)
    
    old_invites = db.get("guild_invites", {}).get(guild_id, {})
    try:
        new_invites = await guild.invites()
    except Exception:
        return

    inviter = None
    for inv in new_invites:
        if inv.code in old_invites and inv.uses > old_invites[inv.code]:
            inviter = inv.inviter
            break
        elif inv.code not in old_invites and inv.uses > 0:
            inviter = inv.inviter
            break

    db["guild_invites"][guild_id] = {inv.code: inv.uses for inv in new_invites}

    if inviter and not inviter.bot:
        inviter_id = str(inviter.id)
        if inviter_id not in db["invites"]:
            db["invites"][inviter_id] = {"joins": 0, "leaves": 0, "fake": 0}

        # Check account age for fake detection (under 7 days)
        account_age = (datetime.now(timezone.utc) - member.created_at).days
        if account_age < 7:
            db["invites"][inviter_id]["fake"] = db["invites"][inviter_id].get("fake", 0) + 1
        else:
            db["invites"][inviter_id]["joins"] = db["invites"][inviter_id].get("joins", 0) + 1

        db["member_inviter"][str(member.id)] = inviter_id

    save_db(db)

@bot.event
async def on_member_remove(member):
    db = load_db()
    member_id = str(member.id)
    inviter_id = db.get("member_inviter", {}).get(member_id)

    if inviter_id and inviter_id in db["invites"]:
        db["invites"][inviter_id]["leaves"] = db["invites"][inviter_id].get("leaves", 0) + 1
        save_db(db)

def get_user_real_invites(user_id: int) -> int:
    db = load_db()
    data = db.get("invites", {}).get(str(user_id), {"joins": 0, "leaves": 0, "fake": 0})
    joins = data.get("joins", 0)
    leaves = data.get("leaves", 0)
    fake = data.get("fake", 0)
    return max(0, joins - leaves - fake)

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------
@bot.command(name="about")
async def cmd_about(ctx):
    embed = discord.Embed(
        title="ℹ️ ABOUT HOSTING SERVER",
        description=(
            "🚀 **IT IS A VERY GOOD HOSTING SERVER YOU WILL GET MANY PLANS OF**\n\n"
            "• **VPS**\n"
            "• **MINECRAFT SERVER HOSTING**\n"
            "• **BOT HOSTING**\n\n"
            "DEVELOPED BY **SKYDOXD**"
        ),
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

@bot.command(name="plans")
async def cmd_plans(ctx):
    db = load_db()
    embed = discord.Embed(title="🛒 PAID VPS PLANS", color=discord.Color.purple())
    for key in sorted(db.get("plans", {}).keys(), key=lambda x: int(x)):
        p = db["plans"][key]
        embed.add_field(
            name=f"Plan {p['id']}: {p['name']}",
            value=f"• **RAM:** `{p['ram']}`\n• **CPU:** `{p['cpu']}` Core(s)\n• **Disk:** `{p['disk']}`\n• **Price:** `${p.get('price')}`",
            inline=False
        )
    embed.set_footer(text="Create a ticket to buy!")
    await ctx.send(embed=embed)

@bot.command(name="customize-plans", aliases=["customize_plans", "customizeplans"])
@is_admin()
async def cmd_customize_plans(ctx, plan_id: str, name: str, ram: str, cpu: int, disk: str, val: str):
    """Syntax: $customize-plans <id> <name> <ram> <cpu> <disk> <price_or_invites>"""
    db = load_db()
    
    if plan_id in db.get("plans", {}):
        db["plans"][plan_id] = {
            "id": int(plan_id), "name": name, "ram": ram, "cpu": cpu, "disk": disk, "price": val
        }
    elif plan_id in db.get("invite_plans", {}):
        try:
            invites_num = int(val)
        except ValueError:
            await ctx.send("❌ For invite reward plans, the value must be a number representing invites.")
            return
        db["invite_plans"][plan_id] = {
            "id": int(plan_id), "name": name, "ram": ram, "cpu": cpu, "disk": disk, "invites": invites_num
        }
    else:
        await ctx.send("❌ **Plan ID not found.** Valid IDs: `1-3` (Paid), `4-6` (Invites).")
        return

    save_db(db)
    await ctx.send(f"✅ **Plan `{plan_id}` updated successfully!**")

@bot.command(name="invite")
async def cmd_invite(ctx, target: discord.Member = None):
    user = target or ctx.author
    db = load_db()
    data = db.get("invites", {}).get(str(user.id), {"joins": 0, "leaves": 0, "fake": 0})
    
    joins = data.get("joins", 0)
    leaves = data.get("leaves", 0)
    fake = data.get("fake", 0)
    total = max(0, joins - leaves - fake)

    embed = discord.Embed(title=f"📩 Invite Stats for {user.display_name}", color=discord.Color.green())
    embed.add_field(name="Real Invites", value=f"`{total}`", inline=True)
    embed.add_field(name="Joins", value=f"`{joins}`", inline=True)
    embed.add_field(name="Leaves", value=f"`{leaves}`", inline=True)
    embed.add_field(name="Fake", value=f"`{fake}`", inline=True)
    embed.set_footer(text="Monitoring invites 24/7.")
    await ctx.send(embed=embed)

# --- INVITE REWARDS INTERACTIVE DROPDOWN MENU ---
class InviteRewardsSelect(Select):
    def __init__(self, invite_plans):
        options = []
        for key in sorted(invite_plans.keys(), key=lambda x: int(x)):
            p = invite_plans[key]
            options.append(
                discord.SelectOption(
                    label=f"{p['name']} (ID {p['id']})",
                    description=f"Requires {p['invites']} Invites | {p['ram']} RAM / {p['cpu']} CPU",
                    value=str(p['id'])
                )
            )
        super().__init__(placeholder="Choose your invite-reward plan...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        plan_id = self.values[0]
        db = load_db()
        plan = db.get("invite_plans", {}).get(plan_id)

        if not plan:
            await interaction.response.send_message("❌ Invalid plan selected.", ephemeral=True)
            return

        user_invites = get_user_real_invites(user.id)
        req_invites = plan["invites"]

        if user_invites < req_invites:
            await interaction.response.send_message(
                f"❌ **Not enough invites!** You have `{user_invites}` real invites, but **{plan['name']}** requires `{req_invites}` invites.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"⚡ **Claiming {plan['name']}...** Triggering automated deployment!",
            ephemeral=True
        )

        # Trigger automatic deployment command logic
        ctx = await bot.get_context(interaction.message)
        await ctx.invoke(
            bot.get_command("create"),
            ram=plan["ram"],
            cpu=int(plan["cpu"]),
            disk=plan["disk"],
            os_type="Ubuntu22.04",
            user=user
        )

class InviteRewardsView(View):
    def __init__(self, invite_plans):
        super().__init__(timeout=180)
        self.add_item(InviteRewardsSelect(invite_plans))

@bot.command(name="invite-rewards", aliases=["invite_rewards", "inviterewards"])
async def cmd_invite_rewards(ctx):
    db = load_db()
    invite_plans = db.get("invite_plans", {})

    embed = discord.Embed(title="🎁 FREE INVITE REWARD VPS PLANS", color=discord.Color.gold())
    for key in sorted(invite_plans.keys(), key=lambda x: int(x)):
        p = invite_plans[key]
        embed.add_field(
            name=f"Plan {p['id']}: {p['name']}",
            value=f"• **RAM:** `{p['ram']}`\n• **CPU:** `{p['cpu']}` Core(s)\n• **Disk:** `{p['disk']}`\n• **Required Invites:** `{p['invites']}`",
            inline=False
        )
    embed.set_footer(text="Select a plan from the dropdown menu below to claim!")

    view = InviteRewardsView(invite_plans)
    await ctx.send(embed=embed, view=view)

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
