import quart
import aiohttp
import os
import json
import asyncio
import traceback
import tempfile
import hmac
import hashlib


if not os.path.exists("config.json"):
    open("config.json", "wb").write(json.dumps({
        "server": {
            "host": "0.0.0.0",
            "port": 8016
        },
        "github": {
            "url": "https://www.github.com/OpenDarkBASIC/OpenDarkBASIC",
            "secret": ""
        },
        "odbc": {
            "compiler_timeout": 3,
            "program_timeout": 5
        }
    }, indent=2).encode("utf-8"))
    print("Created file config.json. Please edit it with the correct settings now, then run the script again")
    import sys
    sys.exit(1)


config = json.loads(open("config.json", "rb").read().decode("utf-8"))
app = quart.Quart(__name__)
lock = asyncio.Lock()

cachedir = "cache"
srcdir = os.path.join(cachedir, "odb-source")
instdir = os.path.join(cachedir, "odb-install")
workdir = os.path.join(cachedir, "work")

if not os.path.exists(cachedir):
    os.mkdir(cachedir)
if not os.path.exists(instdir):
    os.mkdir(instdir)
if not os.path.exists(workdir):
    os.mkdir(workdir)


def verify_signature(payload, signature, secret):
    payload_signature = hmac.new(
            key=secret.encode("utf-8"),
            msg=payload,
            digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(payload_signature, signature)


async def pull_new_odb_version():
    async with lock:
        # Fetch sources
        if not os.path.exists(srcdir):
            git_process = await asyncio.create_subprocess_exec("git", "clone", config["github"]["url"], srcdir)
            if not await git_process.wait():
                return False
        else:
            git_process = await asyncio.create_subprocess_exec("git", "pull", cwd=srcdir)
            if not await git_process.wait():
                return False

        # configure
        builddir = os.path.join(srcdir, "build")
        if not os.path.exists(builddir):
            os.mkdir(builddir)
        cmake_process = await asyncio.create_subprocess_exec("cmake", "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_INSTALL_PREFIX={instdir}", "../", cwd=builddir)
        if not await cmake_process.wait():
            return False

        # build
        make_process = await asyncio.create_subprocess_exec("make", "-j8", cwd=builddir)
        if not await make_process.wait():
            return False

        # install
        make_process = await asyncio.create_subprocess_exec("make", "install", cwd=builddir)
        if not await make_process.wait():
            return False

    return True


async def compile_dbp_source(code):
    compiler = os.path.join(config["dbp"]["path"], "Compiler", "DBPCompiler.exe")
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "source.dba"), "wb") as f:
            f.write(code.encode("utf-8"))

        mm = mmap.mmap(0, 256, "DBPROEDITORMESSAGE")
        compiler_process = await asyncio.create_subprocess_exec(compiler, "source.dba", cwd=tmpdir)
        try:
            await asyncio.wait_for(compiler_process.wait(), config["dbp"]["compiler_timeout"])
        except asyncio.TimeoutError:
            error_msg = mm.read().decode("utf-8").strip("\n")
            compiler_process.terminate()
            await asyncio.sleep(2)  # have to wait for the process to actually terminate, or windows won't delete tmpdir
            return False, error_msg

        program_process = await asyncio.create_subprocess_exec(
            os.path.join(tmpdir, "default.exe"),
            stdout=asyncio.subprocess.PIPE,
            cwd=tmpdir)
        try:
            await asyncio.wait_for(program_process.wait(), config["dbp"]["program_timeout"])
            out = await program_process.stdout.read()
            return True, out.decode("utf-8")
        except asyncio.TimeoutError:
            program_process.terminate()
            await asyncio.sleep(2)  # have to wait for the process to actually terminate, or windows won't delete tmpdir
            return False, f"Executable didn't terminate after {config['dbp']['program_timeout']}s"


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


@app.route("/github", methods=["POST"])
async def github_event():
    payload = await quart.request.get_data()
    if not verify_signature(payload, quart.request.headers["X-Hub-Signature-256"].replace("sha256=", ""), config["github"]["secret"]):
        quart.abort(403)

    event_type = quart.request.headers["X-GitHub-Event"]
    if not event_type == "push":
        return "", 200

    # only care about pushes to master branch
    data = json.loads(payload.decode("utf-8"))
    if not data["ref"].rsplit("/", 1)[-1] == "master":
        return ""

    return "", 200


loop = asyncio.get_event_loop()
try:
    app.run(loop=loop, host=config["server"]["host"], port=config["server"]["port"])
except KeyboardInterrupt:
    pass
except:
    traceback.print_exc()
finally:
    loop.close()
