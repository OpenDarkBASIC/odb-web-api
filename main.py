import quart
import os
import json
import asyncio
import traceback
import tempfile


if not os.path.exists("config.json"):
    open("config.json", "wb").write(json.dumps({
        "server": {
            "host": "0.0.0.0",
            "port": 8015
        },
        "odbc": {
            "compiler_timeout": 3,
            "program_timeout": 5
        },
        "github": {
            "url": "https://www.github.com/OpenDarkBASIC/OpenDarkBASIC"
        }
    }, indent=2).encode("utf-8"))
    print("Created file config.json. Please edit it with the correct settings now, then run the script again")
    import sys
    sys.exit(1)


config = json.loads(open("config.json", "rb").read().decode("utf-8"))
app = quart.Quart(__name__)
lock = asyncio.Lock()

cachedir = os.path.abspath("cache")
srcdir = os.path.join(cachedir, "odb-source")
instdir = os.path.join(cachedir, "odb-install")

if not os.path.exists(cachedir):
    os.mkdir(cachedir)
if not os.path.exists(instdir):
    os.mkdir(instdir)


async def linux_update_odb():
    import multiprocessing
    async with lock:
        # Fetch sources
        if not os.path.exists(srcdir):
            git_process = await asyncio.create_subprocess_exec("git", "clone", config["github"]["url"], srcdir)
            if await git_process.wait():
                return False, "git clone failed"
        else:
            git_process = await asyncio.create_subprocess_exec("git", "pull", cwd=srcdir)
            if await git_process.wait():
                return False, "git pull failed"

        # configure
        builddir = os.path.join(srcdir, "build")
        if not os.path.exists(builddir):
            os.mkdir(builddir)
        cmake_process = await asyncio.create_subprocess_exec(
            "cmake",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={instdir}",
            "-DODBCOMPILER_LLVM_ENABLE_SHARED_LIBS=ON",
            "-DODBCOMPILER_TESTS=OFF",
            "../",
            cwd=builddir)
        if await cmake_process.wait():
            return False, "cmake command failed"

        # build
        make_process = await asyncio.create_subprocess_exec(
            "cmake",
            "--build", ".",
            "--config", "Release",
            "--parallel", f"{multiprocessing.cpu_count()}",
            cwd=builddir)
        if await make_process.wait():
            return False, "build failed"

        # install
        make_process = await asyncio.create_subprocess_exec(
            "cmake",
            "--build", ".",
            "--config", "Release",
            "--target", "install",
            cwd=builddir)
        if await make_process.wait():
            return False, "install failed"

    return True, ""


async def linux_get_commit_hash():
    async with lock:
        if not os.path.exists(os.path.join(instdir, "odbc")):
            return 0
        process = await asyncio.create_subprocess_exec(
            "./odbc", "--no-banner", "--commit-hash",
            stderr=asyncio.subprocess.PIPE,
            cwd=instdir)
        out = await process.stderr.read()
        retcode = await process.wait()
        if retcode != 0:
            return 0

        out = out.decode("utf-8")
        pos = out.find("Commit hash: ")
        if pos == -1:
            return 0
        return out[pos + len("Commit hash: "):].split('\n', 1)[0]


async def compile_dbp_source(code):
    compiler = os.path.join(instdir, "odbc")
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "source.dba"), "wb") as f:
            f.write(code.encode("utf-8"))

        compiler_process = await asyncio.create_subprocess_exec(
            compiler,
            "--no-banner",
            "--no-color",
            "--parse-dba", "source.dba",
            "-o", "program",
            stderr=asyncio.subprocess.PIPE,
            cwd=tmpdir)
        try:
            result = await asyncio.wait_for(compiler_process.wait(), config["odbc"]["compiler_timeout"])
            out = await compiler_process.stderr.read()
            if result != 0:
                return False, out.decode("utf-8")
        except asyncio.TimeoutError:
            compiler_process.terminate()
            return False, f"Compiler took longer than {config['odbc']['compiler_timeout']}s to complete, aborting"

        program_process = await asyncio.create_subprocess_exec(
            os.path.join("./program"),
            stdout=asyncio.subprocess.PIPE,
            cwd=tmpdir)
        try:
            await asyncio.wait_for(program_process.wait(), config["odbc"]["program_timeout"])
            out = await program_process.stdout.read()
            return True, out.decode("utf-8")
        except asyncio.TimeoutError:
            program_process.terminate()
            return False, f"Executable didn't terminate after {config['odbc']['program_timeout']}s, aborting"


@app.route("/compile", methods=["POST"])
async def do_compile():
    payload = await quart.request.get_data()
    payload = json.loads(payload.decode("utf-8"))
    code = payload["code"].replace("\r", "").replace("\n", "\r\n")
    success, output = await compile_dbp_source(code)
    output = output.replace("\r\n", "\n")
    return {
        "success": success,
        "output": output
    }


@app.route("/commit_hash")
async def commit_hash():
    return {
        "commit_hash": await linux_get_commit_hash()
    }


@app.route("/update")
async def update():
    success, msg = await linux_update_odb()
    return {
        "success": success,
        "message": msg
    }


loop = asyncio.get_event_loop()
try:
    app.run(loop=loop, host=config["server"]["host"], port=config["server"]["port"])
except KeyboardInterrupt:
    pass
except:
    traceback.print_exc()
finally:
    loop.close()
