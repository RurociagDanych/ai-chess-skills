import subprocess
import sys
import time


mode = sys.argv[1] if len(sys.argv) > 1 else "valid"
move_count = 0


def analysis_response(count: int) -> tuple[str, list[str], str]:
    if count == 0:
        return "cp 20", ["e2e4", "e7e5"], "e2e4"
    if count == 1:
        return "cp 15", ["e7e5", "g1f3"], "e7e5"
    if count == 2:
        return "cp 20", ["g1f3", "b8c6"], "g1f3"
    if count == 3:
        return "cp 25", ["g8f6", "d2d4"], "g8f6"
    if count == 4:
        return "cp 10", ["d1h5", "g8f6"], "d1h5"
    if count == 5:
        return "cp 320", ["g8f6", "h5f7"], "g8f6"
    if count == 6:
        return "mate 1", ["h5f7"], "h5f7"
    return "mate 1", [], "0000"

for line in sys.stdin:
    command = line.strip()
    if mode == "timeout":
        time.sleep(60)
    elif mode == "flood":
        sys.stdout.buffer.write(b"x" * (2 * 1024 * 1024))
        sys.stdout.buffer.flush()
    elif mode == "invalid-bytes":
        sys.stdout.buffer.write(b"\xff\xfe\nuciok\nreadyok\n")
        sys.stdout.buffer.flush()
        break
    elif mode == "spawn-child-malformed":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        with open(sys.argv[2], "w", encoding="ascii") as output:
            output.write(str(child.pid))
        print("id name FixtureFish 1.0", flush=True)
        break
    elif command == "uci":
        if mode == "malformed":
            print("id name FixtureFish 1.0", flush=True)
        elif mode == "malformed-option":
            print("id name FixtureFish 1.0", flush=True)
            print("option name Foo type", flush=True)
            print("uciok", flush=True)
        elif mode == "integer-version":
            print("id name Stockfish 17", flush=True)
            print("uciok", flush=True)
        else:
            print("id name FixtureFish 1.0", flush=True)
            print("option name Threads type spin default 1 min 1 max 1024", flush=True)
            print("option name Hash type spin default 16 min 1 max 131072", flush=True)
            print("uciok", flush=True)
    elif command == "isready":
        if mode not in {"malformed", "spawn-child-malformed"}:
            print("readyok", flush=True)
    elif command.startswith("setoption name "):
        continue
    elif command.startswith("position "):
        if " moves " in command:
            move_count = len(command.split(" moves ", 1)[1].split())
        else:
            move_count = 0
    elif command.startswith("go depth "):
        depth = int(command.split()[2])
        score, pv, bestmove = analysis_response(move_count)
        pv_suffix = f" pv {' '.join(pv)}" if pv else ""
        print(f"info depth {depth} score {score}{pv_suffix}", flush=True)
        print(f"bestmove {bestmove}", flush=True)
    elif command == "quit":
        break
