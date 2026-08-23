import os
import sys
import json
import time
import re
import secrets
import string
import asyncio
import logging
from datetime import datetime
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

# Default Configuration Setup
if not os.path.exists(CONFIG_FILE):
    default_config = {
        "TOKEN": "YOUR_DISCORD_BOT_TOKEN_HERE",
        "PREFIX": "$",
        "ADMIN_IDS": [],
        "ANTINUKE_ENABLED": True,
        "DEFAULT_DATA_DIR": "./vps_data"
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(default_config, f, indent=4)

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

# Database Initialization
def load_db():
    default_db = {
        "vps": {},
        "admins": config.get("ADMIN_IDS", []),
        "antinuke": config.get("ANTINUKE_ENABLED", True),
        "about_text": "🚀 IT IS A VERY GOOD HOSTING SERVER YOU WILL GET MANY PLANS OF\n\nVPS\nMINECRAFT SERVER HOSTING\nBOT HOSTING \n\nDEVELOPED BY SKYDOXD",
        "plans": {
            "1": {"name": "BASIC PLAN", "ram": "2gb", "cpu": 1, "disk": "10gb", "price": "$1", "type": "paid"},
            "2": {"name": "PRO PLAN", "ram": "5gb", "cpu": 2, "disk": "20gb", "price": "$5", "type": "paid"},
            "3": {"name": "HYPER PLAN", "ram": "8gb", "cpu": 4, "disk": "30gb", "price": "$10", "type": "paid"},
            "4": {"name": "Plan 1", "ram": "1gb", "cpu": 2, "disk": "10gb", "price": "10 invites", "invites_required": 10, "type": "invite"},
            "5": {"name": "Plan 2", "ram": "2gb", "cpu": 1, "disk": "10gb", "price": "15 invites", "invites_required": 15, "type": "invite"},
            "6": {"name": "Plan 3", "ram": "3gb", "cpu": 1, "disk": "10gb", "price": "20 invites", "invites_required": 20, "type": "invite"}
        },
        "invites": {},
        "invite_vps_users": []
    }
    if not os.path.exists(DB_FILE):
        return default_db
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            for k, v in default_db.items():
                if k not in data:
                    data[k] = v
            return data
    except json.JSONDecodeError:
        return default_db

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

try:
    docker_client = docker.from_env()
    logging.info("Connected to Docker daemon successfully.")
except Exception as err:
    logging.error(f"Failed to connect to Docker daemon: {err}")
    docker_client = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True

bot = commands.Bot(command_prefix=config.get("PREFIX", "$"), intents=intents)

# Cache for real-time invite tracking
invite_cache = {}

def generate_password(length=14):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_size_to_bytes(size_str: str) -> int:
    size_str = size_str.lower().strip()
    match = re.match(r"^(\d+)([mg])$", size_str)
    if not match:
        raise ValueError("Invalid format. Use numbers followed by 'm' or 'g' (e.g., 512m, 1g, 10g).")
    num, unit = match.groups()
    num = int(num)
    bytes_val = num * 1024 * 1024 if unit == "m" else num * 1024 * 1024 * 1024
    if bytes_val < 256 * 1024 * 1024:
        raise ValueError("Memory allocation too small. Specify at least 256m.")
    return bytes_val

def is_admin():
    async def predicate(ctx):
        db = load_db()
        admins = db.get("admins", [])
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
# RELIABLE CONTAINER PROVISIONING ENGINE
# ---------------------------------------------------------
def _provision_vps_sync(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    data_dir = os.path.abspath(config.get("DEFAULT_DATA_DIR", "./vps_data"))
    os.makedirs(f"{data_dir}/{container_name}", exist_ok=True)
    
    nano_cpus = int(cpu_cores * 1_000_000_000)

    try:
        docker_client.images.get(image_tag)
    except docker.errors.ImageNotFound:
        logging.info(f"Image {image_tag} not found locally. Pulling...")
        docker_client.images.pull(image_tag)

    try:
        docker_client.images.get("tailscale/tailscale:latest")
    except docker.errors.ImageNotFound:
        docker_client.images.pull("tailscale/tailscale:latest")

    # LXCFS mounts to enforce strict system resource recognition (neofetch/htop)
    bind_mounts = {f"{data_dir}/{container_name}": {"bind": "/data", "mode": "rw"}}
    if os.path.exists("/var/lib/lxcfs/proc"):
        for proc_file in ["meminfo", "cpuinfo", "stat", "uptime"]:
            if os.path.exists(f"/var/lib/lxcfs/proc/{proc_file}"):
                bind_mounts[f"/var/lib/lxcfs/proc/{proc_file}"] = {"bind": f"/proc/{proc_file}", "mode": "rw"}

    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command="bash -c 'apt-get update && apt-get install -y openssh-server neofetch curl && mkdir -p /var/run/sshd && echo \"root:" + root_password + "\" | chpasswd && sed -i \"s/#PermitRootLogin.*/PermitRootLogin yes/g\" /etc/ssh/sshd_config && /usr/sbin/sshd -D'",
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        memswap_limit=ram_bytes,
        nano_cpus=nano_cpus,
        volumes=bind_mounts,
        privileged=True
    )

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
        raise RuntimeError("Failed to capture Tailscale Auth URL.")

    return container, auth_url

async def provision_vps(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    return await asyncio.to_thread(_provision_vps_sync, image_tag, ram_bytes, cpu_cores, disk_bytes, container_name, root_password)

# ---------------------------------------------------------
# BOT EVENTS & INVITE TRACKER
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"SDT-BOTV2 online as {bot.user.name} ({bot.user.id})")
    for guild in bot.guilds:
        try:
            invs = await guild.invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invs}
        except Exception as e:
            logging.error(f"Could not load invites for guild {guild.id}: {e}")

@bot.event
async def on_member_join(member):
    guild = member.guild
    db = load_db()
    
    old_invites = invite_cache.get(guild.id, {})
    try:
        new_invites = await guild.invites()
    except Exception:
        return

    inviter = None
    for inv in new_invites:
        if inv.code in old_invites and inv.uses > old_invites[inv.code]:
            inviter = inv.inviter
            break

    invite_cache[guild.id] = {inv.code: inv.uses for inv in new_invites}

    if inviter and not inviter.bot:
        u_id = str(inviter.id)
        if u_id not in db["invites"]:
            db["invites"][u_id] = {"joins": 0, "leaves": 0, "fake": 0}

        # Check account age for fake detection (<7 days)
        account_age = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
        if account_age < 7:
            db["invites"][u_id]["fake"] += 1
        else:
            db["invites"][u_id]["joins"] += 1

        save_db(db)

@bot.event
async def on_member_remove(member):
    guild = member.guild
    db = load_db()
    # Check leaves
    for u_id, stats in db.get("invites", {}).items():
        if stats.get("joins", 0) > 0:
            stats["leaves"] += 1
            save_db(db)
            break

# ---------------------------------------------------------
# UI INTERACTIVE DROPDOWN FOR INVITE REWARDS
# ---------------------------------------------------------
class InviteRewardSelect(discord.ui.Select):
    def __init__(self):
        db = load_db()
        options = []
        for pid, plan in db.get("plans", {}).items():
            if plan.get("type") == "invite":
                req = plan.get("invites_required", 0)
                options.append(discord.SelectOption(
                    label=f"{plan['name']} (ID {pid})",
                    description=f"{plan['ram']} RAM | {plan['cpu']} CPU | {plan['disk']} Disk - {req} Invites",
                    value=str(pid)
                ))
        if not options:
            options.append(discord.SelectOption(label="No invite plans available", value="none"))

        super().__init__(placeholder="Choose a VPS plan...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("❌ No plans available to claim.", ephemeral=True)
            return

        plan_id = self.values[0]
        db = load_db()
        plan = db["plans"].get(plan_id)
        user_id = str(interaction.user.id)

        # Check if user already claimed an invite VPS
        if interaction.user.id in db.get("invite_vps_users", []):
            await interaction.response.send_message("❌ **Limit Reached:** You have already claimed a free invite-reward VPS. Only admins can grant additional instances.", ephemeral=True)
            return

        inv_stats = db["invites"].get(user_id, {"joins": 0, "leaves": 0, "fake": 0})
        total_valid = inv_stats["joins"] - inv_stats["leaves"] - inv_stats["fake"]
        total_valid = max(0, total_valid)

        req_invites = plan.get("invites_required", 0)

        if total_valid < req_invites:
            embed = discord.Embed(
                title="❌ Claim Denied",
                description=f"You need at least **{req_invites} invites** to claim **{plan['name']}**.\nYour current score: **{total_valid} valid invites**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(f"⏳ **Claiming {plan['name']}... Deploying your VPS!**", ephemeral=True)
        
        ctx = await bot.get_context(interaction.message)
        ctx.author = interaction.user

        # Create VPS via automated execution
        ram = plan['ram']
        cpu = plan['cpu']
        disk = plan['disk']
        os_type = "Ubuntu22.04"
        
        db["invite_vps_users"].append(interaction.user.id)
        save_db(db)

        await ctx.invoke(bot.get_command("create"), ram=ram, cpu=cpu, disk=disk, os_type=os_type, user=interaction.user)

class InviteRewardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(InviteRewardSelect())

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------

@bot.command(name="about")
async def cmd_about(ctx):
    """Shows server info."""
    db = load_db()
    text = db.get("about_text", "SDT-BOTV2 Hosting")
    embed = discord.Embed(title="ℹ️ About Host", description=text, color=discord.Color.blue())
    await ctx.send(embed=embed)

@bot.command(name="plans")
async def cmd_plans(ctx):
    """Shows all paid hosting plans."""
    db = load_db()
    embed = discord.Embed(title="🛒 Available VPS Plans", color=discord.Color.gold())
    
    for pid, plan in db.get("plans", {}).items():
        if plan.get("type") == "paid":
            embed.add_field(
                name=f"🔹 {plan['name']} (ID {pid})",
                value=f"• **RAM:** {plan['ram']}\n• **CPU:** {plan['cpu']} Core(s)\n• **Disk:** {plan['disk']}\n• **Price:** {plan['price']}",
                inline=False
            )
    embed.set_footer(text="Create a ticket to buy.")
    await ctx.send(embed=embed)

@bot.command(name="invite")
async def cmd_invite(ctx, member: discord.Member = None):
    """Shows user invite statistics."""
    target = member or ctx.author
    db = load_db()
    stats = db["invites"].get(str(target.id), {"joins": 0, "leaves": 0, "fake": 0})
    
    joins = stats["joins"]
    leaves = stats["leaves"]
    fake = stats["fake"]
    total = max(0, joins - leaves - fake)

    embed = discord.Embed(title=f"📥 Invite Statistics for {target.display_name}", color=discord.Color.purple())
    embed.add_field(name="Total Invites", value=f"`{total}`", inline=True)
    embed.add_field(name="Joined", value=f"`{joins}`", inline=True)
    embed.add_field(name="Left", value=f"`{leaves}`", inline=True)
    embed.add_field(name="Fake", value=f"`{fake}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="invite-rewards")
async def cmd_invite_rewards(ctx):
    """Displays claim panel for invite rewards."""
    db = load_db()
    embed = discord.Embed(title="🎁 Invite Reward VPS Panel", description="Choose a plan from the menu below to deploy your free VPS. The bot will verify your invites before deployment.", color=discord.Color.green())
    
    for pid, plan in db.get("plans", {}).items():
        if plan.get("type") == "invite":
            req = plan.get("invites_required", 0)
            embed.add_field(
                name=f"Plan ID {pid}: {plan['name']}",
                value=f"• **RAM:** {plan['ram']} | **CPU:** {plan['cpu']} Core(s) | **Disk:** {plan['disk']}\n• **Required:** `{req} Invites`",
                inline=False
            )

    view = InviteRewardView()
    await ctx.send(embed=embed, view=view)

@bot.command(name="customize-plans")
@is_admin()
async def cmd_customize_plans(ctx, plan_id: str, name: str, ram: str, cpu: int, disk: str, price_or_invites: str):
    """Syntax: $customize-plans <id> <name> <ram> <cpu> <disk> <price/invites>"""
    db = load_db()
    
    is_invite_plan = price_or_invites.isdigit() or "invite" in price_or_invites.lower()
    
    if plan_id not in db["plans"]:
        db["plans"][plan_id] = {}

    db["plans"][plan_id]["name"] = name
    db["plans"][plan_id]["ram"] = ram
    db["plans"][plan_id]["cpu"] = cpu
    db["plans"][plan_id]["disk"] = disk

    if price_or_invites.isdigit():
        db["plans"][plan_id]["price"] = f"{price_or_invites} invites"
        db["plans"][plan_id]["invites_required"] = int(price_or_invites)
        db["plans"][plan_id]["type"] = "invite"
    else:
        db["plans"][plan_id]["price"] = price_or_invites
        db["plans"][plan_id]["type"] = "paid"

    save_db(db)
    await ctx.send(f"✅ **Plan `{plan_id}` successfully updated!**")

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    """Lists active user VPS instances."""
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
                f"**SSH User:** `root` | **Password:** `{info.get('password', 'N/A')}`"
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

    status_msg = await ctx.send(f"⏳ **[1/2]** Provisioning VPS ({ram} RAM, {cpu} Core(s), {disk} Disk) for {user.mention}...")
    
    container_name = f"vps-{user.id}-{int(time.time())}"
    root_password = generate_password()

    try:
        container, login_url = await asyncio.wait_for(
            provision_vps(image_tag, ram_bytes, cpu, disk_bytes, container_name, root_password),
            timeout=90.0
        )

        await status_msg.edit(content=f"⏳ **[2/2]** Delivering Tailscale access link to {user.mention} via DM...")

        vps_id = container.id[:10]

        dm_embed = discord.Embed(
            title="🚀 Your VPS is Ready!",
            description="Authorize this instance using the link below to add it to your Tailscale network and receive its global IPv4 address.",
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
            await status_msg.edit(content=f"❌ **Deployment Aborted:** Could not DM {user.mention}. Direct Messages must be enabled.")
            return

        db = load_db()
        db["vps"][vps_id] = {
            "container_id": container.id,
            "container_name": container_name,
            "owner_id": user.id,
            "owner_tag": str(user),
            "ram": ram,
            "cpu": cpu,
            "disk": disk,
            "os": os_key,
            "password": root_password,
            "status": "ACTIVE",
            "created_at": datetime.utcnow().isoformat()
        }
        save_db(db)

        await status_msg.edit(content=f"✅ **VPS Provisioned Successfully!**\n**ID:** `{vps_id}`\n**Assigned To:** {user.mention}\n📩 **Login link delivered to Direct Messages.**")

    except Exception as err:
        logging.error(f"Error provisioning VPS: {err}", exc_info=True)
        await status_msg.edit(content=f"❌ **Deployment Failed:** `{err}`")

if __name__ == "__main__":
    bot.run(config.get("TOKEN"))
