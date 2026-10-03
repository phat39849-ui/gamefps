import asyncio
import json
import os
import random
import string
import logging
import time
from websockets.server import serve

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("server")

PORT = int(os.environ.get("PORT", 8080))
MAX_PLAYERS = 6

class Player:
    def __init__(self, ws, name, slot):
        self.ws = ws
        self.name = name
        self.slot = slot
        self.is_host = False
        self.hp = 100
        self.kills = 0
        self.team = -1

    def to_dict(self):
        return {
            "slot": self.slot,
            "name": self.name,
            "host": self.is_host,
            "kills": self.kills,
            "hp": self.hp,
            "team": self.team
        }

class Room:
    def __init__(self, code, mode):
        self.code = code
        self.mode = mode
        self.players = {}  # slot -> Player
        self.started = False
        self.target = 5
        self.seed = 0
        self.teams = []

    def get_next_slot(self):
        for i in range(MAX_PLAYERS):
            if i not in self.players:
                return i
        return -1

    def get_roster(self):
        roster = [None] * MAX_PLAYERS
        for slot, p in self.players.items():
            roster[slot] = p.to_dict()
        return roster

    async def broadcast(self, msg, exclude_slot=None):
        if isinstance(msg, dict):
            msg = json.dumps(msg)
        coros = []
        for slot, p in self.players.items():
            if slot != exclude_slot:
                coros.append(p.ws.send(msg))
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

rooms = {}  # code -> Room

def generate_code():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choices(chars, k=4))
        if code not in rooms:
            return code

async def handle_client(ws):
    player = None
    room = None
    
    try:
        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            t = data.get("t")

            if t == "HOST":
                if room:
                    continue
                name = data.get("name", "HOST")
                mode = data.get("mode", "ffa")
                code = generate_code()
                room = Room(code, mode)
                rooms[code] = room
                player = Player(ws, name, 0)
                player.is_host = True
                room.players[0] = player
                logger.info(f"Room {code} created by {name}")
                await ws.send(json.dumps({
                    "t": "WELCOME", "code": code, "slot": 0, "isHost": True,
                    "roster": room.get_roster(), "mode": mode, "target": room.target
                }))

            elif t == "JOIN":
                if room:
                    continue
                code = data.get("code", "").upper()
                name = data.get("name", "PLAYER")
                if code not in rooms:
                    await ws.send(json.dumps({"t": "ERR", "msg": "Không tìm thấy phòng đó."}))
                    continue
                r = rooms[code]
                if len(r.players) >= MAX_PLAYERS:
                    await ws.send(json.dumps({"t": "FULL", "msg": "Phòng đã đủ người."}))
                    continue
                if r.started:
                    await ws.send(json.dumps({"t": "ERR", "msg": "Trận đấu đang diễn ra."}))
                    continue
                
                slot = r.get_next_slot()
                player = Player(ws, name, slot)
                r.players[slot] = player
                room = r
                logger.info(f"Player {name} joined room {code} at slot {slot}")
                
                await ws.send(json.dumps({
                    "t": "WELCOME", "code": code, "slot": slot, "isHost": False,
                    "roster": room.get_roster(), "mode": room.mode, "target": room.target
                }))
                await room.broadcast({"t": "ROSTER", "roster": room.get_roster(), "mode": room.mode})

            elif t == "MODE":
                if room and player and player.is_host:
                    mode = data.get("mode", "ffa")
                    room.mode = mode
                    await room.broadcast({"t": "MODE", "mode": mode})
                    await room.broadcast({"t": "ROSTER", "roster": room.get_roster(), "mode": room.mode})

            elif t == "START":
                if room and player and player.is_host:
                    room.started = True
                    room.seed = data.get("seed", 0)
                    room.target = data.get("target", 5)
                    room.teams = data.get("teams", [])
                    if room.teams and len(room.teams) == MAX_PLAYERS:
                        for s, p in room.players.items():
                            p.team = room.teams[s]
                    
                    await room.broadcast({
                        "t": "START", "seed": room.seed, "target": room.target,
                        "teams": room.teams, "roster": room.get_roster(), "mode": room.mode
                    })

            elif t == "S":
                if room and player:
                    msg = {
                        "t": "S", "from": player.slot,
                        "x": data.get("x"), "y": data.get("y"), "z": data.get("z"),
                        "r": data.get("r"), "a": data.get("a"), "s": data.get("s"), "h": data.get("h")
                    }
                    player.hp = data.get("h", player.hp)
                    await room.broadcast(msg, exclude_slot=player.slot)

            elif t == "HIT":
                if room and player:
                    target_slot = data.get("target")
                    if target_slot in room.players:
                        target = room.players[target_slot]
                        await target.ws.send(json.dumps({
                            "t": "DMG", "from": player.slot,
                            "damage": data.get("damage"), "head": data.get("head")
                        }))

            elif t == "KILL":
                if room and player:
                    killer = data.get("from")
                    victim = data.get("victim")
                    head = data.get("head", 0)
                    if killer in room.players:
                        room.players[killer].kills += 1
                    await room.broadcast({"t": "KILL", "from": killer, "victim": victim, "head": head})

            elif t == "REMATCH":
                if room and player and player.is_host:
                    room.started = False
                    for p in room.players.values():
                        p.kills = 0
                        p.hp = 100
                    await room.broadcast({"t": "ROSTER", "roster": room.get_roster(), "mode": room.mode})

            elif t == "PING":
                await ws.send(json.dumps({"t": "PONG", "ts": data.get("ts")}))

    except Exception as e:
        logger.error(f"Error handling client: {e}")
    finally:
        if room and player:
            slot = player.slot
            name = player.name
            if slot in room.players:
                del room.players[slot]
            logger.info(f"Player {name} left room {room.code}")
            
            if not room.players:
                logger.info(f"Room {room.code} is empty, deleting.")
                if room.code in rooms:
                    del rooms[room.code]
            else:
                if player.is_host:
                    new_host_slot = min(room.players.keys())
                    room.players[new_host_slot].is_host = True
                    logger.info(f"Host transferred to slot {new_host_slot} in room {room.code}")
                
                await room.broadcast({"t": "ROSTER", "roster": room.get_roster(), "mode": room.mode})
                if room.started:
                    await room.broadcast({"t": "LEAVE", "slot": slot, "name": name})

async def main():
    async with serve(handle_client, "0.0.0.0", PORT):
        logger.info(f"Server started on port {PORT}")
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass