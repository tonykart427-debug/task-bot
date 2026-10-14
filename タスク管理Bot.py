import os
import json
import datetime as dt

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 任意。入れるとコマンドがすぐ反映される
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))  # 毎朝の通知時刻(日本時間)

JST = dt.timezone(dt.timedelta(hours=9))
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "tasks.json")


# ---------- データの保存と読み込み ----------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"next_id": 1, "tasks": [], "channel_id": None}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_due(text):
    """'2026-10-20' または '10-20' を受け付ける。空ならNone。失敗したらValueError。"""
    if not text:
        return None
    text = text.strip()
    today = dt.datetime.now(JST).date()
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    month, day = text.replace("/", "-").split("-")
    d = dt.date(today.year, int(month), int(day))
    if d < today:
        d = dt.date(today.year + 1, int(month), int(day))
    return d.isoformat()


def format_task(t):
    mark = "✅" if t["done"] else "⬜"
    due = f"(期限 {t['due']})" if t.get("due") else ""
    return f"{mark} #{t['id']} {t['text']} {due}".strip()


def sort_key(t):
    return (t["done"], t.get("due") or "9999-12-31", t["id"])


# ---------- ボット本体 ----------
class TaskBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(group)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        morning_reminder.start()

    async def on_ready(self):
        print(f"ログインしました: {self.user}")
        await catch_up()


client = TaskBot()
group = app_commands.Group(name="task", description="タスク管理")


@group.command(name="add", description="タスクを追加します")
@app_commands.describe(text="タスクの内容", due="期限(例: 10-20 または 2026-10-20)。省略可")
async def task_add(interaction: discord.Interaction, text: str, due: str = ""):
    try:
        due_value = parse_due(due)
    except Exception:
        await interaction.response.send_message(
            "期限の形式が正しくありません。例: 10-20 または 2026-10-20", ephemeral=True
        )
        return
    data = load_data()
    task = {
        "id": data["next_id"],
        "user": interaction.user.id,
        "text": text,
        "due": due_value,
        "done": False,
    }
    data["tasks"].append(task)
    data["next_id"] += 1
    save_data(data)
    await interaction.response.send_message(f"追加しました: {format_task(task)}")


@group.command(name="list", description="自分のタスク一覧を表示します")
@app_commands.describe(show_done="完了済みも表示する")
async def task_list(interaction: discord.Interaction, show_done: bool = False):
    data = load_data()
    mine = [t for t in data["tasks"] if t["user"] == interaction.user.id]
    if not show_done:
        mine = [t for t in mine if not t["done"]]
    if not mine:
        await interaction.response.send_message("タスクはありません。", ephemeral=True)
        return
    mine.sort(key=sort_key)
    await interaction.response.send_message(
        "\n".join(format_task(t) for t in mine)[:1900], ephemeral=True
    )


@group.command(name="done", description="タスクを完了にします")
@app_commands.describe(task_id="タスクの番号(#の後の数字)")
async def task_done(interaction: discord.Interaction, task_id: int):
    data = load_data()
    for t in data["tasks"]:
        if t["id"] == task_id and t["user"] == interaction.user.id:
            t["done"] = True
            save_data(data)
            await interaction.response.send_message(f"完了にしました: {format_task(t)}")
            return
    await interaction.response.send_message("その番号のタスクが見つかりません。", ephemeral=True)


@group.command(name="delete", description="タスクを削除します")
@app_commands.describe(task_id="タスクの番号(#の後の数字)")
async def task_delete(interaction: discord.Interaction, task_id: int):
    data = load_data()
    before = len(data["tasks"])
    data["tasks"] = [
        t for t in data["tasks"]
        if not (t["id"] == task_id and t["user"] == interaction.user.id)
    ]
    if len(data["tasks"]) == before:
        await interaction.response.send_message("その番号のタスクが見つかりません。", ephemeral=True)
        return
    save_data(data)
    await interaction.response.send_message(f"#{task_id} を削除しました。")


@group.command(name="setchannel", description="毎朝のリマインドを送るチャンネルを、今のチャンネルに設定します")
async def task_setchannel(interaction: discord.Interaction):
    data = load_data()
    data["channel_id"] = interaction.channel_id
    save_data(data)
    await interaction.response.send_message(
        f"毎朝{MORNING_HOUR}時のリマインドをこのチャンネルに送ります。"
    )


# ---------- 毎朝のリマインド ----------
async def send_reminders():
    """今日のリマインドを送り、送った日付を記録する。送れなければFalse。"""
    data = load_data()
    if not data.get("channel_id"):
        return False
    channel = client.get_channel(data["channel_id"])
    if channel is None:
        return False
    today = dt.datetime.now(JST).date().isoformat()
    users = sorted({t["user"] for t in data["tasks"] if not t["done"]})
    for uid in users:
        mine = sorted(
            [t for t in data["tasks"] if t["user"] == uid and not t["done"]], key=sort_key
        )
        lines = []
        for t in mine:
            line = format_task(t)
            if t.get("due") and t["due"] < today:
                line += " ⚠️期限切れ"
            elif t.get("due") == today:
                line += " 🔔今日まで"
            lines.append(line)
        await channel.send(f"<@{uid}> おはようございます。今日のタスクです。\n" + "\n".join(lines)[:1800])
    data = load_data()
    data["last_reminder"] = today
    save_data(data)
    return True


@tasks.loop(time=dt.time(hour=MORNING_HOUR, minute=0, tzinfo=JST))
async def morning_reminder():
    await send_reminders()


async def catch_up():
    """再起動や更新で朝の時刻を逃した場合、起動時に今日の分を送る。"""
    now = dt.datetime.now(JST)
    data = load_data()
    if now.hour >= MORNING_HOUR and data.get("last_reminder") != now.date().isoformat():
        await send_reminders()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(".env に DISCORD_TOKEN を設定してください")
    client.run(TOKEN)
