from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

LOGGER = logging.getLogger("rolimons_monitor")
STATE_PATH = Path(os.getenv("STATE_FILE", "state.json"))


@dataclass(frozen=True)
class Config:
    discord_token: str
    root_place_id: int
    poll_seconds: int
    timezone_name: str
    owner_names: set[str]
    owner_ids: set[int]
    sync_guild_id: int | None


def read_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing. Put it into .env first.")

    owner_ids = {
        int(value.strip())
        for value in os.getenv("OWNER_USER_IDS", "").split(",")
        if value.strip().isdigit()
    }
    owner_names = {
        value.strip().lower()
        for value in os.getenv("OWNER_USERNAMES", "yandexik,minekotik").split(",")
        if value.strip()
    }

    sync_guild_raw = os.getenv("SYNC_GUILD_ID", "").strip()
    sync_guild_id = int(sync_guild_raw) if sync_guild_raw.isdigit() else None

    return Config(
        discord_token=token,
        root_place_id=int(os.getenv("ROOT_PLACE_ID", "18186775539")),
        poll_seconds=max(30, int(os.getenv("POLL_SECONDS", "60"))),
        timezone_name=os.getenv("TIMEZONE", "Europe/Chisinau"),
        owner_names=owner_names,
        owner_ids=owner_ids,
        sync_guild_id=sync_guild_id,
    )


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"selected_channels": {}, "snapshot": None}

    try:
        with STATE_PATH.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (json.JSONDecodeError, OSError):
        LOGGER.exception("Could not read state file; starting with an empty state.")
        return {"selected_channels": {}, "snapshot": None}

    state.setdefault("selected_channels", {})
    state.setdefault("snapshot", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(STATE_PATH)


def parse_roblox_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    plus_at = max(text.rfind("+"), text.rfind("-"))
    if "." in text:
        dot_at = text.find(".")
        tz_part = text[plus_at:] if plus_at > dot_at else ""
        main_part = text[:plus_at] if plus_at > dot_at else text
        seconds, fraction = main_part.split(".", 1)
        text = f"{seconds}.{fraction[:6]}{tz_part}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        LOGGER.warning("Could not parse Roblox datetime: %s", value)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def discord_time(value: str | None, timezone_name: str) -> str:
    parsed = parse_roblox_datetime(value)
    if parsed is None:
        return "неизвестно"

    local_tz = ZoneInfo(timezone_name)
    local = parsed.astimezone(local_tz)
    unix = int(parsed.timestamp())
    return f"{local:%Y-%m-%d %H:%M:%S %Z} (<t:{unix}:R>)"


def price_text(value: Any) -> str:
    if value is None:
        return "нет цены"
    return str(value)


class RobloxClient:
    def __init__(self, root_place_id: int):
        self.root_place_id = root_place_id
        self.session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(8)

    async def __aenter__(self) -> "RobloxClient":
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "Discord Roblox update monitor"},
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.session:
            await self.session.close()

    async def request_json(self, url: str, **kwargs: Any) -> Any:
        if self.session is None:
            raise RuntimeError("RobloxClient session is not open.")

        for attempt in range(4):
            async with self._sem:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429 or response.status >= 500:
                        await asyncio.sleep(2**attempt)
                        continue
                    response.raise_for_status()
                    return await response.json()

        async with self.session.get(url, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def request_bytes(self, url: str, **kwargs: Any) -> bytes | None:
        if self.session is None:
            raise RuntimeError("RobloxClient session is not open.")

        async with self._sem:
            async with self.session.get(url, **kwargs) as response:
                if response.status != 200:
                    return None
                return await response.read()

    async def get_game_icon(self, universe_id: int) -> bytes | None:
        try:
            data = await self.request_json(
                "https://thumbnails.roblox.com/v1/games/icons",
                params={
                    "universeIds": str(universe_id),
                    "size": "512x512",
                    "format": "Png",
                    "isCircular": "false",
                },
            )
            items = data.get("data") or []
            if not items:
                return None
            image_url = items[0].get("imageUrl")
            if not image_url:
                return None
            return await self.request_bytes(image_url)
        except Exception:
            LOGGER.exception("Could not fetch game icon.")
            return None

    async def get_universe_id(self) -> int:
        data = await self.request_json(
            f"https://apis.roblox.com/universes/v1/places/{self.root_place_id}/universe"
        )
        return int(data["universeId"])

    async def get_game(self, universe_id: int) -> dict[str, Any]:
        data = await self.request_json(
            "https://games.roblox.com/v1/games",
            params={"universeIds": str(universe_id)},
        )
        games = data.get("data") or []
        if not games:
            raise RuntimeError(f"Game for universe {universe_id} was not found.")
        game = games[0]
        return {
            "id": str(game["id"]),
            "root_place_id": str(game["rootPlaceId"]),
            "name": game["name"],
            "updated": game.get("updated"),
        }

    async def get_places(self, universe_id: int) -> list[dict[str, Any]]:
        places: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params = {"sortOrder": "Asc", "limit": "100"}
            if cursor:
                params["cursor"] = cursor

            data = await self.request_json(
                f"https://develop.roblox.com/v1/universes/{universe_id}/places",
                params=params,
            )
            places.extend(data.get("data", []))
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

        async def enrich(place: dict[str, Any]) -> dict[str, Any]:
            details = await self.request_json(
                f"https://economy.roblox.com/v2/assets/{place['id']}/details"
            )
            return {
                "id": str(place["id"]),
                "name": details.get("Name") or place.get("name") or str(place["id"]),
                "updated": details.get("Updated"),
            }

        return await asyncio.gather(*(enrich(place) for place in places))

    async def get_gamepasses(self, universe_id: int) -> list[dict[str, Any]]:
        passes: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params = {"sortOrder": "Asc", "limit": "100"}
            if page_token:
                params["pageToken"] = page_token

            data = await self.request_json(
                f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes",
                params=params,
            )
            passes.extend(data.get("gamePasses", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        async def enrich(gamepass: dict[str, Any]) -> dict[str, Any]:
            details = await self.request_json(
                f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass['id']}/product-info"
            )
            return {
                "id": str(gamepass["id"]),
                "name": details.get("Name") or gamepass.get("name") or str(gamepass["id"]),
                "updated": details.get("Updated") or gamepass.get("updated"),
                "price": details.get("PriceInRobux"),
                "is_for_sale": details.get("IsForSale"),
            }

        return await asyncio.gather(*(enrich(gamepass) for gamepass in passes))

    async def get_badges(self, universe_id: int) -> list[dict[str, Any]]:
        badges: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params = {"sortOrder": "Asc", "limit": "100"}
            if cursor:
                params["cursor"] = cursor

            data = await self.request_json(
                f"https://badges.roblox.com/v1/universes/{universe_id}/badges",
                params=params,
            )
            badges.extend(data.get("data", []))
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

        return [
            {
                "id": str(badge["id"]),
                "name": badge.get("name") or badge.get("displayName") or str(badge["id"]),
                "updated": badge.get("updated"),
                "enabled": badge.get("enabled"),
                "description": badge.get("description"),
                "icon_image_id": badge.get("iconImageId"),
            }
            for badge in badges
        ]

    async def get_snapshot(self) -> dict[str, Any]:
        universe_id = await self.get_universe_id()
        game, places, gamepasses, badges = await asyncio.gather(
            self.get_game(universe_id),
            self.get_places(universe_id),
            self.get_gamepasses(universe_id),
            self.get_badges(universe_id),
        )
        return {
            "universe_id": str(universe_id),
            "game": game,
            "places": {place["id"]: place for place in places},
            "gamepasses": {gamepass["id"]: gamepass for gamepass in gamepasses},
            "badges": {badge["id"]: badge for badge in badges},
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def compact_line(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def diff_named_items(
    old_items: dict[str, dict[str, Any]],
    new_items: dict[str, dict[str, Any]],
    item_label: str,
) -> list[str]:
    lines: list[str] = []

    for item_id, new in sorted(new_items.items(), key=lambda entry: entry[1].get("name", "")):
        old = old_items.get(item_id)
        if old is None:
            lines.append(f"+ Добавился {item_label}: {compact_line(new['name'])}")
            continue

        if old.get("name") != new.get("name"):
            lines.append(f"- {old.get('name')} -> {new.get('name')}")
        elif old != new:
            lines.append(f"- {compact_line(new['name'])} изменен")

    for item_id, old in sorted(old_items.items(), key=lambda entry: entry[1].get("name", "")):
        if item_id not in new_items:
            lines.append(f"- Удалился {item_label}: {compact_line(old['name'])}")

    return lines


def diff_snapshot(old: dict[str, Any] | None, new: dict[str, Any], timezone_name: str) -> list[str]:
    if not old:
        return []

    sections: list[tuple[str, list[str]]] = []

    game_lines: list[str] = []
    if old["game"].get("updated") != new["game"].get("updated"):
        game_lines.append(
            f"{new['game']['name']}: {discord_time(old['game'].get('updated'), timezone_name)} -> "
            f"{discord_time(new['game'].get('updated'), timezone_name)}"
        )
    if game_lines:
        sections.append(("GAME", game_lines))

    place_lines: list[str] = []
    old_places = old.get("places", {})
    new_places = new.get("places", {})
    for place_id, place in sorted(new_places.items(), key=lambda entry: entry[1].get("name", "")):
        previous = old_places.get(place_id)
        if previous is None:
            place_lines.append(
                f"+ Добавился плейс: {place['name']} - {discord_time(place.get('updated'), timezone_name)}"
            )
            continue

        name_changed = previous.get("name") != place.get("name")
        updated_changed = previous.get("updated") != place.get("updated")
        if name_changed and updated_changed:
            place_lines.append(
                f"- {previous.get('name')} -> {place.get('name')}: "
                f"{discord_time(previous.get('updated'), timezone_name)} -> "
                f"{discord_time(place.get('updated'), timezone_name)}"
            )
        elif name_changed:
            place_lines.append(f"- {previous.get('name')} -> {place.get('name')}")
        elif updated_changed:
            place_lines.append(
                f"- {place['name']}: {discord_time(previous.get('updated'), timezone_name)} -> "
                f"{discord_time(place.get('updated'), timezone_name)}"
            )

    for place_id, previous in sorted(old_places.items(), key=lambda entry: entry[1].get("name", "")):
        if place_id not in new_places:
            place_lines.append(f"- Удалился плейс: {previous['name']}")

    if place_lines:
        sections.append(("PLACES", place_lines))

    gamepass_lines: list[str] = []
    old_passes = old.get("gamepasses", {})
    new_passes = new.get("gamepasses", {})
    for pass_id, gamepass in sorted(new_passes.items(), key=lambda entry: entry[1].get("name", "")):
        previous = old_passes.get(pass_id)
        if previous is None:
            gamepass_lines.append(
                f"+ Добавился новый геймпасс: {gamepass['name']} (цена: {price_text(gamepass.get('price'))})"
            )
            continue

        changes: list[str] = []
        if previous.get("name") != gamepass.get("name"):
            changes.append(f"имя: {previous.get('name')} -> {gamepass.get('name')}")
        if previous.get("price") != gamepass.get("price"):
            changes.append(
                f"цена: {price_text(previous.get('price'))} -> {price_text(gamepass.get('price'))}"
            )
        if previous.get("is_for_sale") != gamepass.get("is_for_sale"):
            changes.append(
                f"продажа: {previous.get('is_for_sale')} -> {gamepass.get('is_for_sale')}"
            )
        if not changes and previous != gamepass:
            changes.append("изменен")
        if changes:
            gamepass_lines.append(f"- {gamepass['name']}: {'; '.join(changes)}")

    for pass_id, previous in sorted(old_passes.items(), key=lambda entry: entry[1].get("name", "")):
        if pass_id not in new_passes:
            gamepass_lines.append(f"- Удалился геймпасс: {previous['name']}")

    if gamepass_lines:
        sections.append(("GAMEPASSES", gamepass_lines))

    badge_lines = diff_named_items(old.get("badges", {}), new.get("badges", {}), "бейдж")
    if badge_lines:
        sections.append(("BADGES", badge_lines))

    if not sections:
        return []

    lines = ["**Обнаружено обновление Roblox-игры**", f"Ссылка: https://www.rolimons.com/game/{new['game']['root_place_id']}"]
    for title, section_lines in sections:
        lines.append("")
        lines.append(f"**{title}**")
        lines.extend(section_lines)
    return lines


def build_check_message(snapshot: dict[str, Any], timezone_name: str) -> list[str]:
    lines = [
        f"**{snapshot['game']['name']}**",
        f"Игра обновлена: {discord_time(snapshot['game'].get('updated'), timezone_name)}",
        "",
        "**PLACES**",
    ]

    places = sorted(snapshot.get("places", {}).values(), key=lambda place: place.get("name", ""))
    for place in places:
        marker = " (стартовый)" if place["id"] == snapshot["game"]["root_place_id"] else ""
        lines.append(f"- {place['name']}{marker}: {discord_time(place.get('updated'), timezone_name)}")

    return split_discord_messages(lines)


def split_discord_messages(lines: list[str], limit: int = 1900) -> list[str]:
    messages: list[str] = []
    current = ""
    for line in lines:
        addition = f"{line}\n"
        if current and len(current) + len(addition) > limit:
            messages.append(current.rstrip())
            current = ""
        current += addition
    if current.strip():
        messages.append(current.rstrip())
    return messages


def user_can_select(user: discord.abc.User, config: Config) -> bool:
    if user.id in config.owner_ids:
        return True

    names = {
        getattr(user, "name", "").lower(),
        getattr(user, "display_name", "").lower(),
        (getattr(user, "global_name", None) or "").lower(),
    }
    return bool(names & config.owner_names)


config = read_config()
state = load_state()

# message_content больше не нужен: слэш-командам не требуется читать текст сообщений.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    LOGGER.info("Logged in as %s", bot.user)

    try:
        if config.sync_guild_id:
            guild = discord.Object(id=config.sync_guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            LOGGER.info("Synced %d slash command(s) to guild %s.", len(synced), config.sync_guild_id)
        else:
            synced = await bot.tree.sync()
            LOGGER.info("Synced %d slash command(s) globally (may take up to 1h to appear).", len(synced))
    except Exception:
        LOGGER.exception("Failed to sync slash commands.")

    if not monitor_loop.is_running():
        monitor_loop.start()


@bot.tree.command(name="select", description="Выбрать этот канал для публикации обновлений.")
async def select_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда `/select` работает только на сервере.", ephemeral=True)
        return

    if not user_can_select(interaction.user, config):
        await interaction.response.send_message(
            "Эту команду могут использовать только yandexik и minekotik.", ephemeral=True
        )
        return

    state["selected_channels"][str(interaction.guild.id)] = interaction.channel.id
    save_state(state)
    await interaction.response.send_message(f"Готово, буду писать обновления в {interaction.channel.mention}.")


@bot.tree.command(
    name="checkupdate",
    description="Проверить, есть ли обновления в игре (доступно только вне сервера).",
)
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def check_update(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    try:
        async with RobloxClient(config.root_place_id) as client:
            snapshot = await client.get_snapshot()
    except Exception:
        LOGGER.exception("checkupdate failed.")
        await interaction.followup.send("Не смог проверить Roblox/Rolimon's прямо сейчас. Попробуй чуть позже.")
        return

    old_snapshot = state.get("snapshot")
    update_lines = diff_snapshot(old_snapshot, snapshot, config.timezone_name)

    if not update_lines:
        await interaction.followup.send("Обновлений нет — всё как при последней проверке.")
        return

    # Снимок не сохраняем: пусть фоновый мониторинг сам решает, когда фиксировать
    # изменения и рассылать их по серверам, чтобы не было пропусков там.
    for message in split_discord_messages(update_lines):
        await interaction.followup.send(message)


@bot.tree.command(name="check", description="Проверить состояние Roblox-игры прямо сейчас.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def check_now(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    try:
        async with RobloxClient(config.root_place_id) as client:
            snapshot = await client.get_snapshot()
    except Exception:
        LOGGER.exception("Manual check failed.")
        await interaction.followup.send("Не смог проверить Roblox/Rolimon's прямо сейчас. Попробуй чуть позже.")
        return

    messages = build_check_message(snapshot, config.timezone_name)
    for message in messages:
        await interaction.followup.send(message)


@tasks.loop(seconds=config.poll_seconds)
async def monitor_loop() -> None:
    try:
        async with RobloxClient(config.root_place_id) as client:
            snapshot = await client.get_snapshot()
    except Exception:
        LOGGER.exception("Background check failed.")
        return

    old_snapshot = state.get("snapshot")
    update_lines = diff_snapshot(old_snapshot, snapshot, config.timezone_name)

    state["snapshot"] = snapshot
    save_state(state)

    if not update_lines:
        return

    messages = split_discord_messages(update_lines)
    for guild_id, channel_id in list(state.get("selected_channels", {}).items()):
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            LOGGER.warning("Selected channel %s for guild %s was not found.", channel_id, guild_id)
            continue

        for message in messages:
            await channel.send(message)


@monitor_loop.before_loop
async def before_monitor_loop() -> None:
    await bot.wait_until_ready()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

    while True:
        try:
            LOGGER.info("Starting bot...")
            bot.run(config.discord_token)
        except KeyboardInterrupt:
            LOGGER.info("Bot stopped by user.")
            break
        except Exception:
            LOGGER.exception("Bot crashed! Restarting in 10 seconds...")
            import time
            time.sleep(10)


if __name__ == "__main__":
    main()