import logging
import os
import re
import secrets
from typing import Annotated, Literal
import json
import math

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import AfterValidator, BaseModel, Field
from rcon.source import Client

load_dotenv()

# Configuration
ADDRESS = os.getenv("ADDRESS", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))
API_KEY = os.environ["API_KEY"]

RCON_ADDRESS = os.getenv("RCON_ADDRESS", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ["RCON_PASSWORD"]

if not API_KEY or not RCON_PASSWORD:
    raise RuntimeError("API_KEY and RCON_PASSWORD must not be empty")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("minecraft-api")

app = FastAPI(title="Minecraft HTTP API")

# Validate identifiers before inserting them into server commands.
RESOURCE_RE = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")


def to_snbt(value) -> str:
    """Convert JSON-compatible Python values to Minecraft SNBT."""

    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(k, ensure_ascii=False)}:{to_snbt(v)}"
            for k, v in value.items()
        ) + "}"

    if isinstance(value, list):
        return "[" + ",".join(to_snbt(v) for v in value) + "]"

    if isinstance(value, bool):
        return "1b" if value else "0b"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite NBT number")
        return f"{value}d"

    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)

    raise ValueError(f"Unsupported NBT value: {type(value).__name__}")


def normalise_resource(value: str) -> str:
    if ":" not in value:
        value = f"minecraft:{value}"
    if not RESOURCE_RE.fullmatch(value):
        raise ValueError("Expected an item/entity/effect ID such as minecraft:diamond")
    return value


ResourceID = Annotated[str, AfterValidator(normalise_resource)]
PlayerName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{3,16}$")]


def authenticate(authorization: str | None = Header(default=None)):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorised")


def send_commands(commands: list[str], port: int | None = None) -> dict:
    rcon_port = RCON_PORT if port is None else port
    try:
        with Client(RCON_ADDRESS, rcon_port, passwd=RCON_PASSWORD) as rcon:
            results = [rcon.run(command) for command in commands]
    except Exception as exc:
        logger.exception("RCON command failed")
        raise HTTPException(
            status_code=502,
            detail="Failed to communicate with Minecraft",
        ) from exc

    return {
        "command_sent": True,
        "commands_sent": len(commands),
        "rcon_port": rcon_port,
        "rcon_response": results[0] if len(results) == 1 else results,
    }


class RCONRequest(BaseModel):
    # Override the Minecraft RCON port for this HTTP request only.
    port: int | None = Field(default=None, ge=1, le=65535)


class DropRequest(RCONRequest):
    item: ResourceID = "minecraft:diamond"
    count: int = Field(default=1, ge=1, le=64)
    player: PlayerName | None = None


class GiveRequest(RCONRequest):
    item: ResourceID = "minecraft:diamond"
    count: int = Field(default=1, ge=1, le=2304)
    player: PlayerName | None = None
    custom_name: str | None = Field(default=None, max_length=256)
    nbt_data: dict | None = None

class SummonRequest(RCONRequest):
    entity: ResourceID = "minecraft:zombie"
    count: int = Field(default=1, ge=1, le=10)
    player: PlayerName | None = None
    at_player: bool = False
    nbt_data: dict | None = None
    custom_name: str | None = Field(default=None, max_length=256)
    name_visible: bool = True


class EffectRequest(RCONRequest):
    effect: ResourceID = "minecraft:speed"
    duration: Annotated[int, Field(ge=1, le=1_000_000)] | Literal["infinite"] = 30
    amplifier: int = Field(default=0, ge=0, le=255)
    hide_particles: bool = False
    player: PlayerName | None = None


class WeatherRequest(RCONRequest):
    weather: Literal["clear", "rain", "thunder"]
    duration: int | None = Field(default=None, ge=1, le=86400)


class TimeRequest(RCONRequest):
    time: Literal["day", "noon", "night", "midnight"] | Annotated[
        int, Field(ge=0, le=2_147_483_647)
    ] = "day"


class XPRequest(RCONRequest):
    amount: int = Field(default=10, ge=-1_000_000, le=1_000_000)
    unit: Literal["points", "levels"] = "points"
    player: PlayerName | None = None

class DamageRequest(RCONRequest):
    amount: float = Field(default=1, ge=0)
    type: Literal["generic", "fire", "fall", "explosion"] = "generic"
    player: PlayerName | None = None

@app.post("/damage", dependencies=[Depends(authenticate)])
def apply_damage(data: DamageRequest):
    target = data.player or "@a"
    damage_type = data.type or "minecraft:generic"
    command = f"execute as {target} run damage @s {data.amount} {damage_type}"
    logger.info("Applying %s %s damage to %s", data.amount, damage_type, target)
    return {
        "amount": data.amount,
        "type": damage_type,
        "target": target,
        **send_commands([command], port=data.port),
    }

@app.get("/health")
def health():
    return {"status": "online"}


@app.post("/spawn", dependencies=[Depends(authenticate)], include_in_schema=False)
@app.post("/drop", dependencies=[Depends(authenticate)])
def drop_item(data: DropRequest):
    target = data.player or "@a"
    command = (
        f"execute at {target} run summon minecraft:item ~ ~1 ~ "
        f'{{Item:{{id:"{data.item}",count:{data.count}}}}}'
    )
    logger.info("Dropping %d %s for %s", data.count, data.item, target)
    return {
        "item": data.item,
        "count": data.count,
        "target": target,
        **send_commands([command], port=data.port),
    }




@app.post("/give", dependencies=[Depends(authenticate)])
def give_item(data: GiveRequest):
    target = data.player or "@a"
    item = data.item
    components = []

    # Minecraft display name
    if data.custom_name is not None:
        text_component = json.dumps({
            "text": data.custom_name,
            "italic": False,
        }, ensure_ascii=False)

        components.append(
            f"minecraft:custom_name={to_snbt(text_component)}"
        )

    # Arbitrary custom NBT data
    if data.nbt_data is not None:
        try:
            nbt = to_snbt(data.nbt_data)
            components.append(f"minecraft:custom_data={nbt}")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if components:
        item += f"[{','.join(components)}]"

    command = f"give {target} {item} {data.count}"

    logger.info(
        "Giving %d %s to %s (Name: %s, NBT: %s)",
        data.count,
        data.item,
        target,
        data.custom_name,
        data.nbt_data,
    )

    return {
        "item": data.item,
        "count": data.count,
        "target": target,
        "custom_name": data.custom_name,
        "nbt_data": data.nbt_data,
        **send_commands([command], port=data.port),
    }





@app.post("/summon", dependencies=[Depends(authenticate)])
def summon_entity(data: SummonRequest):
    target = data.player or "@a"
    position = "~ ~ ~" if data.at_player else "~2 ~ ~"

    # Copy NBT to avoid modifying the original request data.
    nbt_data = dict(data.nbt_data or {})

    # Apply custom name if provided.
    if data.custom_name is not None:
        nbt_data["CustomName"] = json.dumps({
            "text": data.custom_name,
            "italic": False,
        }, ensure_ascii=False)

        nbt_data["CustomNameVisible"] = data.name_visible

    # Convert NBT to SNBT.
    try:
        nbt = f" {to_snbt(nbt_data)}" if nbt_data else ""
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    command = (
        f"execute at {target} run summon "
        f"{data.entity} {position}{nbt}"
    )

    logger.info(
        "Summoning %d %s near %s (Name: %s, NBT: %s)",
        data.count,
        data.entity,
        target,
        data.custom_name,
        nbt_data,
    )

    return {
        "entity": data.entity,
        "count": data.count,
        "target": target,
        "at_player": data.at_player,
        "custom_name": data.custom_name,
        "name_visible": data.name_visible,
        "nbt_data": nbt_data,
        **send_commands([command] * data.count, port=data.port),
    }


@app.post("/effect", dependencies=[Depends(authenticate)])
def apply_effect(data: EffectRequest):
    target = data.player or "@a"
    hidden = str(data.hide_particles).lower()
    command = (
        f"effect give {target} {data.effect} "
        f"{data.duration} {data.amplifier} {hidden}"
    )
    logger.info("Applying %s to %s", data.effect, target)
    return {
        "effect": data.effect,
        "duration": data.duration,
        "amplifier": data.amplifier,
        "target": target,
        **send_commands([command], port=data.port),
    }


@app.post("/weather", dependencies=[Depends(authenticate)])
def change_weather(data: WeatherRequest):
    # In 1.21.1, bare weather durations mean ticks. Append 's' for seconds.
    command = f"weather {data.weather}"
    if data.duration is not None:
        command += f" {data.duration}s"
    logger.info("Changing weather to %s", data.weather)
    return {"weather": data.weather, **send_commands([command], port=data.port)}


@app.post("/time", dependencies=[Depends(authenticate)])
def change_time(data: TimeRequest):
    command = f"time set {data.time}"
    logger.info("Setting time to %s", data.time)
    return {"time": data.time, **send_commands([command], port=data.port)}


@app.post("/xp", dependencies=[Depends(authenticate)])
def change_xp(data: XPRequest):
    # Default to every online player.
    target = data.player or "@a"
    command = f"experience add {target} {data.amount} {data.unit}"
    logger.info("Changing XP by %d %s for %s", data.amount, data.unit, target)
    return {
        "amount": data.amount,
        "unit": data.unit,
        "target": target,
        **send_commands([command], port=data.port),
    }


if __name__ == "__main__":
    uvicorn.run(app, host=ADDRESS, port=PORT)
