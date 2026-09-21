from flask import Flask, request, render_template, send_file, jsonify
import subprocess, os, uuid, json, tempfile, threading, time, sys, glob, shutil
import urllib.request, urllib.error, urllib.parse
import zipfile, io

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Each teammate provides their own key locally in a gitignored .env file
# (GEMINI_API_KEY=...) — never commit this, unlike the Supabase anon key
# above which is safe to embed because it's gated by RLS, not secrecy.
GEMINI_API_KEY      = os.environ.get('GEMINI_API_KEY')
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY')

_tasks = {}   # task_id → {status, progress, result, filename, error}

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 * 1024  # 20 GB max upload

TEMP_DIR = tempfile.gettempdir()
NULL_DEV = 'NUL' if os.name == 'nt' else '/dev/null'

# Every teammate runs their own local copy of this app, so feedback can't
# just live in a local file — it needs to land somewhere shared. This key
# is Supabase's "anon" public key: it's meant to be embedded in distributed
# client code like this. Access is restricted by the table's Row Level
# Security policies (insert/select/update/delete on `feedback` only), not
# by keeping the key secret.
SUPABASE_URL      = 'https://yewhqjkdbmkrzosyuwwa.supabase.co'
SUPABASE_ANON_KEY = ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIs'
                      'InJlZiI6Inlld2hxamtkYm1rcnpvc3l1d3dhIiwicm9sZSI6ImFub24iLCJp'
                      'YXQiOjE3ODUyNTIxNzAsImV4cCI6MjEwMDgyODE3MH0.e9ySko38XKZcrl9H'
                      'vzp6T9XmmZZjQ0avop2gFZAztQM')


def supabase_request(method, path, body=None):
    """Call the Supabase REST (PostgREST) API. `path` includes the table
    name and any query string, e.g. 'feedback' or 'qa_word_feedback?...'."""
    req = urllib.request.Request(
        f'{SUPABASE_URL}/rest/v1/{path}',
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            'apikey':        SUPABASE_ANON_KEY,
            'Authorization': f'Bearer {SUPABASE_ANON_KEY}',
            'Content-Type':  'application/json',
            'Prefer':        'return=representation',
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


# ── Auto-update ───────────────────────────────────────────────────────────────

def auto_update():
    """Pull latest code from GitHub. If updated, restart the app automatically."""
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    # Only run if this is a git repo
    git_dir = os.path.join(repo_dir, '.git')
    if not os.path.exists(git_dir):
        return

    print('🔄  Checking for updates...')
    try:
        # Fetch latest from remote
        subprocess.run(['git', 'fetch', 'origin', 'main'],
                       cwd=repo_dir, capture_output=True, timeout=10)

        # Check if we're behind
        result = subprocess.run(
            ['git', 'rev-list', 'HEAD..origin/main', '--count'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10)

        commits_behind = int(result.stdout.strip() or '0')

        if commits_behind == 0:
            print('✅  App is up to date.')
            return

        # Capture HEAD before pulling so the requirements-changed check below
        # covers every commit being pulled, not just the last one — with
        # multiple commits behind, diffing HEAD~1..HEAD only sees the final
        # commit's file changes and can miss a requirements.txt change from
        # earlier in the range.
        old_head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10).stdout.strip()

        print(f'⬇️   {commits_behind} update(s) found. Pulling latest version...')
        subprocess.run(['git', 'pull', 'origin', 'main'],
                       cwd=repo_dir, capture_output=True, timeout=30)

        # Re-install dependencies if a requirements file changed anywhere in
        # the pulled range.
        req_changed = subprocess.run(
            ['git', 'diff', old_head, 'HEAD', '--name-only'],
            cwd=repo_dir, capture_output=True, text=True).stdout
        # EDITOPS_REQUIREMENTS_FILE (set by start_windows_server.bat) takes
        # priority over the OS-based guess below — without it, a lite-server
        # deployment set up with requirements-server.txt would silently
        # reinstall the full requirements-windows.txt on its next update,
        # pulling back in openai-whisper/easyocr that setup deliberately
        # left out.
        req_file = os.environ.get('EDITOPS_REQUIREMENTS_FILE') or \
                   ('requirements-windows.txt' if os.name == 'nt' else 'requirements.txt')
        pip_bin  = os.path.join('venv', 'Scripts', 'pip.exe') if os.name == 'nt' \
                   else os.path.join('venv', 'bin', 'pip')
        pip      = os.path.join(repo_dir, pip_bin)
        if 'requirements' in req_changed:
            print('📦  Updating dependencies...')
            try:
                subprocess.run([pip, 'install', '-r',
                                os.path.join(repo_dir, req_file), '-q'],
                               cwd=repo_dir, timeout=600)
            except Exception as e:
                # Don't let a failed dependency install block the restart
                # below — the new code is already pulled, and several
                # routes already degrade gracefully (clear "missing
                # dependency" error) if a new package didn't get installed.
                print(f'⚠️   Dependency update failed (continuing anyway): {e}')

        print('🔁  Restarting app with latest version...\n')
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        print(f'⚠️   Update check failed (continuing anyway): {e}')


def start_periodic_auto_update():
    """Opt-in only, via EDITOPS_AUTO_UPDATE_HOURS in .env — unset by
    default, so every existing install (Mac and Windows alike) keeps
    today's exact behavior: auto_update() runs once, at startup, and
    that's it. Meant for an unattended server deployment where picking
    up new commits without someone manually restarting it is the whole
    point; not something a teammate actively using their own local copy
    would want, since a restart drops whatever's mid-request at that
    moment.

    Re-checks every EDITOPS_AUTO_UPDATE_HOURS hours via the same
    auto_update() used at startup, but skips (and retries next interval)
    if a task is actively processing when the check fires, rather than
    yanking the process out from under an in-progress job.
    """
    raw = os.environ.get('EDITOPS_AUTO_UPDATE_HOURS')
    if not raw:
        return
    try:
        interval_hours = float(raw)
        if interval_hours <= 0:
            raise ValueError
    except ValueError:
        print(f'⚠️   EDITOPS_AUTO_UPDATE_HOURS={raw!r} is not a valid positive '
              f'number — periodic auto-update disabled.')
        return

    def _loop():
        busy = {'processing', 'processing_audio'}
        while True:
            time.sleep(interval_hours * 3600)
            if any(t.get('status') in busy for t in _tasks.values()):
                print('🔄  Periodic update check skipped — a job is currently '
                      'processing; will retry next interval.')
                continue
            print(f'\n🔄  Periodic update check (every {interval_hours}h)...')
            auto_update()

    threading.Thread(target=_loop, daemon=True).start()
    print(f'🔁  Periodic auto-update enabled — checking every {interval_hours}h.')


# ── Helpers ──────────────────────────────────────────────────────────────────

def cleanup_later(path, delay=90):
    """Delete a temp file after a short delay (gives send_file time to finish)."""
    def _del():
        time.sleep(delay)
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_del, daemon=True).start()


def ffprobe_info(path):
    """Return dict with duration, bit_rate, has_audio, has_video for a media file."""
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    fmt = data.get('format', {})
    streams = data.get('streams', [])
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    return {
        'duration': float(fmt.get('duration') or 0),
        'bit_rate':  int(fmt.get('bit_rate')  or 0),
        'has_audio': audio is not None,
        'has_video': video is not None,
    }


def parse_time(s):
    """Parse 'mm:ss', 'h:mm:ss', or plain seconds string → float seconds."""
    parts = s.strip().split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(s)


def atempo_chain(speed):
    """Build an atempo filter string that handles any speed (outside 0.5–2.0)."""
    filters, r = [], speed
    if speed >= 1.0:
        while r > 2.0:
            filters.append('atempo=2.0')
            r /= 2.0
    else:
        while r < 0.5:
            filters.append('atempo=0.5')
            r *= 2.0
    filters.append(f'atempo={r:.8f}')
    return ','.join(filters)


def run_hw_encode(cmd_prefix, cmd_suffix, bv):
    """Runs `cmd_prefix + vcodec + cmd_suffix`, trying the platform's
    hardware H.264 encoder first — VideoToolbox on macOS, Quick Sync
    (QSV) on Windows, both much faster than software libx264 encoding,
    which matters most on weaker CPUs where this can be the difference
    between usable and painfully slow — then transparently falling back
    to libx264 if the hardware attempt fails (e.g. Quick Sync unavailable
    or misconfigured on a specific Windows machine's GPU drivers) rather
    than failing the whole job over an optional speed optimization that
    isn't guaranteed to work on every machine. Returns the
    CompletedProcess from whichever attempt succeeded (or the last
    failed attempt, if none did).
    """
    hw_vcodec = None
    if sys.platform == 'darwin':
        hw_vcodec = ['-c:v', 'h264_videotoolbox', '-b:v', bv, '-allow_sw', '1']
    elif os.name == 'nt':
        # -preset veryfast: without an explicit preset, QSV defaults to
        # "medium" — clearly slower than intended here, since the whole
        # point of using QSV over libx264 is speed. -async_depth lets the
        # encoder pipeline more frames concurrently through its internal
        # queue instead of waiting on each one. Standard QSV performance-
        # tuning flags (not something testable on this dev machine, which
        # has no Intel Quick Sync hardware) — same automatic libx264
        # fallback covers it if either flag causes trouble on a specific
        # driver/ffmpeg build.
        hw_vcodec = ['-c:v', 'h264_qsv', '-b:v', bv, '-preset', 'veryfast', '-async_depth', '4']

    sw_vcodec = ['-c:v', 'libx264', '-b:v', bv, '-preset', 'fast']

    r = None
    for vcodec in ([hw_vcodec] if hw_vcodec else []) + [sw_vcodec]:
        r = subprocess.run(cmd_prefix + vcodec + cmd_suffix, capture_output=True)
        if r.returncode == 0:
            return r
    return r


def run_hw_encode_crf(cmd_prefix, cmd_suffix, crf=18, preset='fast'):
    """CRF/quality-based counterpart to run_hw_encode(), for routes with
    no fixed bitrate target — e.g. concatenating clips of unknown/mixed
    source bitrate, where "match the original bitrate" doesn't apply.
    On Windows, tries Quick Sync's closest equivalent to CRF
    (-global_quality — same numeric range and "lower is higher quality"
    semantics) first, falling back to plain libx264 CRF if that fails.

    macOS/Linux always use libx264 here unchanged — unlike Speed Up and
    Thumbnail, the routes calling this never had a hardware-encoder
    branch to begin with, so this only adds the new Windows path rather
    than also changing existing behavior elsewhere.
    """
    # -preset veryfast + -async_depth: see run_hw_encode() for why.
    hw_vcodec = ['-c:v', 'h264_qsv', '-global_quality', str(crf), '-look_ahead', '0',
                 '-preset', 'veryfast', '-async_depth', '4'] \
                if os.name == 'nt' else None

    sw_vcodec = ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf)]

    r = None
    for vcodec in ([hw_vcodec] if hw_vcodec else []) + [sw_vcodec]:
        r = subprocess.run(cmd_prefix + vcodec + cmd_suffix, capture_output=True)
        if r.returncode == 0:
            return r
    return r


def save_upload(file, fallback_ext='.mp4'):
    uid = str(uuid.uuid4())
    ext = os.path.splitext(file.filename)[1] or fallback_ext
    path = os.path.join(TEMP_DIR, f'vt_{uid}{ext}')
    file.save(path)
    return path, uid


def stem(filename):
    return os.path.splitext(filename)[0]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/speed', methods=['POST'])
def speed_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No file uploaded'), 400

    input_path, uid = save_upload(file)

    info = ffprobe_info(input_path)
    if not info:
        os.remove(input_path)
        return jsonify(error='Cannot read file. Is it a valid video or audio?'), 400

    mode    = request.form.get('mode', 'multiplier')
    raw     = request.form.get('value', '')
    preview = request.form.get('preview') == '1'

    try:
        if mode == 'duration':
            target = parse_time(raw)
            if target <= 0:
                raise ValueError
            speed = info['duration'] / target
        else:
            speed = float(raw)
            if speed <= 0:
                raise ValueError
    except (ValueError, ZeroDivisionError):
        os.remove(input_path)
        return jsonify(error='Invalid speed / duration value.'), 400

    # -t 60 before -i limits input to 60 s for preview mode
    t_limit = ['-t', '60'] if preview else []

    if not info['has_video']:
        # Audio-only: apply atempo chain, output mp3
        output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp3')
        af = atempo_chain(speed)
        cmd = ['ffmpeg', '-y', *t_limit, '-i', input_path,
               '-filter:a', af, '-c:a', 'libmp3lame', '-b:a', '192k',
               output_path]
        r = subprocess.run(cmd, capture_output=True)
        cleanup_later(input_path)
        if r.returncode != 0:
            return jsonify(error='ffmpeg failed. Make sure ffmpeg is installed.'), 500
        cleanup_later(output_path)
        return send_file(output_path, as_attachment=True,
                         download_name=f'{stem(file.filename)}_sped_up.mp3')

    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')
    vf = f'setpts=PTS/{speed:.8f}'
    if info['has_audio']:
        af = atempo_chain(speed)
        fc   = f'[0:v]{vf}[v];[0:a]{af}[a]'
        maps = ['-map', '[v]', '-map', '[a]']
    else:
        fc   = f'[0:v]{vf}[v]'
        maps = ['-map', '[v]']

    # Match original bitrate so quality is preserved
    bv = f"{max(500, int(info['bit_rate'] * 0.98 / 1000))}k" if info['bit_rate'] else '14M'

    cmd_prefix = ['ffmpeg', '-y', *t_limit, '-i', input_path, '-filter_complex', fc, *maps]
    cmd_suffix = ['-c:a', 'aac', '-ac', '2', '-b:a', '192k', '-movflags', '+faststart', output_path]
    r = run_hw_encode(cmd_prefix, cmd_suffix, bv)
    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='ffmpeg failed. Make sure ffmpeg is installed.'), 500

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True,
                     download_name=f'{stem(file.filename)}_sped_up.mp4')


@app.route('/compress', methods=['POST'])
def compress_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No file uploaded'), 400

    input_path, uid = save_upload(file)

    info = ffprobe_info(input_path)
    if not info or info['duration'] == 0:
        os.remove(input_path)
        return jsonify(error='Cannot read file.'), 400

    target_mb  = float(request.form.get('target_mb', 900))
    total_bits = target_mb * 1_000_000 * 8

    if not info['has_video']:
        # Audio-only: set bitrate directly to hit target size
        abr = max(32_000, int(total_bits / info['duration']))
        abr_k = f"{abr // 1000}k"
        output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp3')
        cmd = ['ffmpeg', '-y', '-i', input_path,
               '-c:a', 'libmp3lame', '-b:a', abr_k, output_path]
        r = subprocess.run(cmd, capture_output=True)
        cleanup_later(input_path)
        if r.returncode != 0:
            return jsonify(error='Compression failed.'), 500
        cleanup_later(output_path)
        return send_file(output_path, as_attachment=True,
                         download_name=f'{stem(file.filename)}_compressed.mp3')

    passlog    = os.path.join(TEMP_DIR, f'vt_pass_{uid}')
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')
    audio_bits = 192_000 * info['duration']
    vbr        = int((total_bits - audio_bits) / info['duration'])

    if vbr <= 0:
        os.remove(input_path)
        return jsonify(error='Target size is too small for this video duration.'), 400

    # Two-pass for accurate file size
    cmd1 = ['ffmpeg', '-y', '-i', input_path,
             '-c:v', 'libx264', '-b:v', str(vbr),
             '-pass', '1', '-passlogfile', passlog,
             '-an', '-f', 'null', NULL_DEV]
    subprocess.run(cmd1, capture_output=True)

    cmd2 = ['ffmpeg', '-y', '-i', input_path,
             '-c:v', 'libx264', '-b:v', str(vbr),
             '-pass', '2', '-passlogfile', passlog,
             '-c:a', 'aac', '-b:a', '192k',
             '-movflags', '+faststart', output_path]
    r = subprocess.run(cmd2, capture_output=True)

    for suf in ['-0.log', '-0.log.mbtree']:
        try: os.remove(passlog + suf)
        except: pass
    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='Compression failed.'), 500

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True,
                     download_name=f'{stem(file.filename)}_compressed.mp4')


@app.route('/trim', methods=['POST'])
def trim_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No file uploaded'), 400

    input_path, uid = save_upload(file)

    try:
        segments = json.loads(request.form.get('segments', '[]'))
    except (ValueError, TypeError):
        os.remove(input_path)
        return jsonify(error='Invalid segments data.'), 400

    if not segments:
        os.remove(input_path)
        return jsonify(error='Please keep at least one segment.'), 400

    info = ffprobe_info(input_path)
    duration = info['duration'] if info else None

    cleaned = []
    for seg in segments:
        try:
            s, e = float(seg['start']), float(seg['end'])
        except (KeyError, TypeError, ValueError):
            os.remove(input_path)
            return jsonify(error='Invalid segment data.'), 400
        if s < 0 or e <= s or (duration and e > duration + 0.5):
            os.remove(input_path)
            return jsonify(error='Segment times are out of range.'), 400
        cleaned.append((s, e))

    if info and not info['has_video']:
        out_ext = os.path.splitext(file.filename)[1] or '.mp3'
    else:
        out_ext = '.mp4'

    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}{out_ext}')

    # Explicit mapping — see /convert for why: implicit stream selection can
    # silently drop audio on some source files (e.g. no track flagged as
    # the "default" one).
    def extract(start, end, out_path):
        cmd = ['ffmpeg', '-y', '-ss', str(start), '-i', input_path, '-to', str(end - start),
               '-map', '0:v:0?', '-map', '0:a:0?', '-c', 'copy', out_path]
        return subprocess.run(cmd, capture_output=True)

    if len(cleaned) == 1:
        r = extract(cleaned[0][0], cleaned[0][1], output_path)
        if r.returncode != 0:
            cleanup_later(input_path)
            return jsonify(error='Trim failed.'), 500
    else:
        segment_paths = []
        for i, (s, e) in enumerate(cleaned):
            seg_path = os.path.join(TEMP_DIR, f'vt_trimseg_{uid}_{i}{out_ext}')
            r = extract(s, e, seg_path)
            if r.returncode != 0:
                cleanup_later(input_path)
                for p in segment_paths:
                    cleanup_later(p)
                return jsonify(error='Trim failed.'), 500
            segment_paths.append(seg_path)

        concat_path = os.path.join(TEMP_DIR, f'vt_trimconcat_{uid}.txt')
        with open(concat_path, 'w') as fh:
            for p in segment_paths:
                fh.write(f"file '{p}'\n")

        # Stream-copy concat is safe here (unlike /merge) because every
        # segment was cut from the same source file, so codec params match.
        r = subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_path,
             '-c', 'copy', output_path],
            capture_output=True)
        for p in segment_paths:
            cleanup_later(p)
        cleanup_later(concat_path)
        if r.returncode != 0:
            cleanup_later(input_path)
            return jsonify(error='Trim failed.'), 500

    cleanup_later(input_path)
    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True,
                     download_name=f'{stem(file.filename)}_trimmed{out_ext}')


@app.route('/trim/waveform', methods=['POST'])
def trim_waveform_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No file uploaded'), 400

    input_path, uid = save_upload(file)
    output_path = os.path.join(TEMP_DIR, f'vt_waveform_{uid}.png')

    r = subprocess.run(
        ['ffmpeg', '-y', '-i', input_path,
         '-filter_complex', 'aformat=channel_layouts=mono,showwavespic=s=1600x100:colors=0x8A8A8A',
         '-frames:v', '1', output_path],
        capture_output=True)
    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='Could not generate waveform.'), 500

    cleanup_later(output_path)
    return send_file(output_path, mimetype='image/png')


@app.route('/trim/thumbnails', methods=['POST'])
def trim_thumbnails_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No file uploaded'), 400

    input_path, uid = save_upload(file)
    info = ffprobe_info(input_path)
    duration = info['duration'] if info else 0

    if not info or not info['has_video'] or duration <= 0:
        cleanup_later(input_path)
        return jsonify(error='No video stream to generate thumbnails from.'), 400

    output_path = os.path.join(TEMP_DIR, f'vt_thumbs_{uid}.png')

    # A sprite of evenly-spaced frames tiled horizontally — used as the
    # timeline background the same way /trim/waveform is, just for video.
    # Sized generously (80 frames across a virtual 3200px timeline) so it
    # still looks reasonable at 2x zoom instead of blocky.
    n = 80
    thumb_w, thumb_h = 40, 100
    r = subprocess.run(
        ['ffmpeg', '-y', '-i', input_path,
         '-vf', f'fps={n / duration},scale={thumb_w}:{thumb_h},tile={n}x1',
         '-frames:v', '1', output_path],
        capture_output=True)
    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='Could not generate thumbnails.'), 500

    cleanup_later(output_path)
    return send_file(output_path, mimetype='image/png')


@app.route('/merge', methods=['POST'])
def merge_route():
    files = request.files.getlist('videos')
    if len(files) < 2:
        return jsonify(error='Please upload at least 2 files.'), 400

    uid = str(uuid.uuid4())
    input_paths = []
    for i, f in enumerate(files):
        ext  = os.path.splitext(f.filename)[1] or '.mp4'
        path = os.path.join(TEMP_DIR, f'vt_merge_{uid}_{i}{ext}')
        f.save(path)
        input_paths.append(path)

    # Detect if any file has a video stream
    def has_video_stream(path):
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', path],
            capture_output=True, text=True)
        if r.returncode != 0:
            return False
        streams = json.loads(r.stdout).get('streams', [])
        return any(s.get('codec_type') == 'video' for s in streams)

    is_video_merge = any(has_video_stream(p) for p in input_paths)

    concat_path = os.path.join(TEMP_DIR, f'vt_concat_{uid}.txt')
    with open(concat_path, 'w') as fh:
        for p in input_paths:
            fh.write(f"file '{p}'\n")

    if is_video_merge:
        output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')
        # Explicit mapping and stereo downmix — see /convert for why.
        cmd_prefix = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_path,
                      '-map', '0:v:0?', '-map', '0:a:0?']
        cmd_suffix = ['-c:a', 'aac', '-ac', '2', '-b:a', '192k', '-movflags', '+faststart', output_path]
        download_name = 'merged_video.mp4'
        r = run_hw_encode_crf(cmd_prefix, cmd_suffix)
    else:
        output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp3')
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_path,
               '-c:a', 'libmp3lame', '-b:a', '192k',
               output_path]
        download_name = 'merged_audio.mp3'
        r = subprocess.run(cmd, capture_output=True)

    cleanup_later(concat_path)
    for p in input_paths:
        cleanup_later(p)

    if r.returncode != 0:
        return jsonify(error='Merge failed.'), 500

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True, download_name=download_name)


@app.route('/thumbnail', methods=['POST'])
def thumbnail_route():
    video_file = request.files.get('video')
    image_file = request.files.get('image')
    if not video_file:
        return jsonify(error='No video uploaded'), 400
    if not image_file:
        return jsonify(error='No thumbnail image uploaded'), 400

    try:
        duration = float(request.form.get('duration', ''))
        if duration <= 0:
            raise ValueError
    except ValueError:
        return jsonify(error='Please enter a valid duration in seconds.'), 400

    position = request.form.get('position', 'start')
    if position not in ('start', 'end'):
        position = 'start'

    video_path, uid = save_upload(video_file)
    img_ext = os.path.splitext(image_file.filename)[1] or '.jpg'
    image_path = os.path.join(TEMP_DIR, f'vt_thumb_{uid}{img_ext}')
    image_file.save(image_path)

    info = ffprobe_info(video_path)
    if not info or not info['has_video']:
        for p in (video_path, image_path):
            try: os.remove(p)
            except: pass
        return jsonify(error='Cannot read video file.'), 400

    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', video_path],
        capture_output=True, text=True)
    streams = json.loads(r.stdout).get('streams', []) if r.returncode == 0 else []
    vstream = next((s for s in streams if s.get('codec_type') == 'video'), None)
    astream = next((s for s in streams if s.get('codec_type') == 'audio'), None)

    width, height = (vstream.get('width'), vstream.get('height')) if vstream else (None, None)
    fps = vstream.get('r_frame_rate', '25/1') if vstream else '25/1'
    if not width or not height:
        for p in (video_path, image_path):
            try: os.remove(p)
            except: pass
        return jsonify(error='Cannot read video dimensions.'), 400

    has_audio   = astream is not None
    sample_rate = astream.get('sample_rate', '44100') if astream else '44100'
    layout      = 'mono' if has_audio and int(astream.get('channels', 2)) == 1 else 'stereo'

    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')

    # Scale+letterbox the image to the video's frame, then splice with concat.
    img_v = (f'[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,'
             f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,'
             f'setsar=1,fps={fps},format=yuv420p[imgv]')
    vid_v = f'[0:v]setsar=1,fps={fps},format=yuv420p[vidv]'

    if has_audio:
        img_a = (f'anullsrc=channel_layout={layout}:sample_rate={sample_rate},'
                 f'atrim=duration={duration}[imga]')
        concat = ('[imgv][imga][vidv][0:a]concat=n=2:v=1:a=1[outv][outa]' if position == 'start'
                  else '[vidv][0:a][imgv][imga]concat=n=2:v=1:a=1[outv][outa]')
        fc   = ';'.join([img_v, vid_v, img_a, concat])
        maps = ['-map', '[outv]', '-map', '[outa]']
    else:
        concat = ('[imgv][vidv]concat=n=2:v=1:a=0[outv]' if position == 'start'
                  else '[vidv][imgv]concat=n=2:v=1:a=0[outv]')
        fc   = ';'.join([img_v, vid_v, concat])
        maps = ['-map', '[outv]']

    # Match original bitrate so quality is preserved (same approach as /speed)
    bv = f"{max(500, int(info['bit_rate'] * 0.98 / 1000))}k" if info['bit_rate'] else '14M'

    cmd_prefix = ['ffmpeg', '-y',
                  '-i', video_path,
                  '-loop', '1', '-t', str(duration), '-i', image_path,
                  '-filter_complex', fc, *maps]
    cmd_suffix = ['-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', output_path]
    r = run_hw_encode(cmd_prefix, cmd_suffix, bv)
    cleanup_later(video_path)
    cleanup_later(image_path)

    if r.returncode != 0:
        return jsonify(error='Failed to stitch thumbnail into video.'), 500

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True,
                     download_name=f'{stem(video_file.filename)}_with_thumbnail.mp4')


@app.route('/convert', methods=['POST'])
def convert_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No video uploaded'), 400

    target_fmt = request.form.get('format', 'mp4').lower().strip('.')
    SUPPORTED = {
        'mp4':  {'vcodec': 'libx264',    'acodec': 'aac',      'ext': '.mp4'},
        'mov':  {'vcodec': 'libx264',    'acodec': 'aac',      'ext': '.mov'},
        'avi':  {'vcodec': 'libxvid',    'acodec': 'mp3',      'ext': '.avi'},
        'mkv':  {'vcodec': 'libx264',    'acodec': 'aac',      'ext': '.mkv'},
        'webm': {'vcodec': 'libvpx-vp9', 'acodec': 'libopus', 'ext': '.webm'},
        'gif':  {'vcodec': None,         'acodec': None,       'ext': '.gif'},
        'mp3':  {'acodec': 'libmp3lame', 'abr': '192k',        'ext': '.mp3', 'audio_only': True},
        'wav':  {'acodec': 'pcm_s16le',  'abr': None,          'ext': '.wav', 'audio_only': True},
    }

    if target_fmt not in SUPPORTED:
        return jsonify(error=f'Unsupported format. Choose from: {", ".join(SUPPORTED)}'), 400

    input_path, uid = save_upload(file)
    cfg = SUPPORTED[target_fmt]
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}{cfg["ext"]}')

    if cfg.get('audio_only'):
        # Downmix to stereo: a 5.1/multichannel source re-encoded without
        # -ac keeps 6 channels but many players choke on that in mp3/wav.
        cmd = ['ffmpeg', '-y', '-i', input_path, '-vn', '-ac', '2', '-c:a', cfg['acodec']]
        if cfg.get('abr'):
            cmd += ['-b:a', cfg['abr']]
        cmd.append(output_path)
        r = subprocess.run(cmd, capture_output=True)
    elif target_fmt == 'gif':
        # High-quality GIF via palette
        palette = os.path.join(TEMP_DIR, f'vt_palette_{uid}.png')
        subprocess.run(
            ['ffmpeg', '-y', '-i', input_path,
             '-vf', 'fps=15,scale=640:-1:flags=lanczos,palettegen', palette],
            capture_output=True)
        r = subprocess.run(
            ['ffmpeg', '-y', '-i', input_path, '-i', palette,
             '-filter_complex', 'fps=15,scale=640:-1:flags=lanczos[x];[x][1:v]paletteuse',
             output_path],
            capture_output=True)
        cleanup_later(palette)
    else:
        # Explicit stream mapping rather than relying on ffmpeg's automatic
        # selection — without it, some source files (e.g. an MKV with no
        # audio track flagged as the "default" one) can end up with ffmpeg
        # not auto-selecting an audio stream at all, silently dropping
        # audio despite the command otherwise looking correct. The "?"
        # suffix makes each map optional so this doesn't hard-fail when a
        # stream type genuinely isn't present.
        #
        # -ac 2: a 5.1/multichannel source (e.g. EAC3 from an MKV) re-encoded
        # to AAC without forcing the channel count keeps 6 channels but with
        # an unrecognized channel layout in the mp4 container — the track is
        # structurally present and ffprobe reports it fine, but many real
        # players (notably Windows' built-in AAC decoder) silently refuse to
        # play it, which reads to a user as "audio completely missing" even
        # though the conversion "succeeded". Downmixing to stereo sidesteps
        # the whole class of multichannel-layout compatibility problems.
        cmd_prefix = ['ffmpeg', '-y', '-i', input_path, '-map', '0:v:0?', '-map', '0:a:0?']
        cmd_suffix = ['-c:a', cfg['acodec'], '-ac', '2', '-b:a', '192k',
                      '-movflags', '+faststart', output_path]
        if cfg['vcodec'] == 'libx264':
            # Only H.264 targets (mp4/mov/mkv) have a Quick Sync
            # equivalent on this hardware — avi (Xvid) and webm (VP9)
            # stay on their existing software encoders unconditionally.
            r = run_hw_encode_crf(cmd_prefix, cmd_suffix)
        else:
            cmd = cmd_prefix + ['-c:v', cfg['vcodec'], '-preset', 'fast', '-crf', '18'] + cmd_suffix
            r = subprocess.run(cmd, capture_output=True)

    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='Conversion failed.'), 500

    cleanup_later(output_path)
    out_name = f'{stem(file.filename)}{cfg["ext"]}'
    return send_file(output_path, as_attachment=True, download_name=out_name)


# ── SVG to After Effects ────────────────────────────────────────────────────
# There's no library that writes a valid .aep from scratch — it's an
# undocumented Adobe binary format. Instead we generate an ExtendScript
# (.jsx) that uses AE's own documented scripting API to build the shape
# layers and save the project; the user runs it inside After Effects via
# File > Scripts > Run Script File.

def _pt(p):
    return [p.x, p.y]


def svg_shape_subpaths(el):
    """Walk one SVG shape's segments into AE-style subpaths: vertices plus
    inTangents/outTangents (relative offsets from their vertex, AE's shape
    path convention), and a closed flag. Arcs are approximated with cubic
    Beziers since AE shape paths have no native arc segment."""
    from svgelements import Path, Move, Close, Line, CubicBezier, QuadraticBezier, Arc

    path = el if isinstance(el, Path) else Path(el)
    subpaths = []
    verts = inT = outT = None
    closed = False

    def flush():
        if verts and len(verts) > 1:
            subpaths.append({'vertices': verts, 'inTangents': inT, 'outTangents': outT, 'closed': closed})

    def add_cubic(c1x, c1y, c2x, c2y, end):
        outT[-1] = [c1x - verts[-1][0], c1y - verts[-1][1]]
        verts.append(_pt(end))
        inT.append([c2x - end.x, c2y - end.y])
        outT.append([0, 0])

    for seg in path.segments():
        if isinstance(seg, Move):
            flush()
            verts, inT, outT, closed = [_pt(seg.end)], [[0, 0]], [[0, 0]], False
        elif isinstance(seg, Close):
            closed = True
        elif isinstance(seg, Line):
            verts.append(_pt(seg.end))
            inT.append([0, 0])
            outT.append([0, 0])
        elif isinstance(seg, CubicBezier):
            add_cubic(seg.control1.x, seg.control1.y, seg.control2.x, seg.control2.y, seg.end)
        elif isinstance(seg, QuadraticBezier):
            c1x = seg.start.x + 2 / 3 * (seg.control.x - seg.start.x)
            c1y = seg.start.y + 2 / 3 * (seg.control.y - seg.start.y)
            c2x = seg.end.x   + 2 / 3 * (seg.control.x - seg.end.x)
            c2y = seg.end.y   + 2 / 3 * (seg.control.y - seg.end.y)
            add_cubic(c1x, c1y, c2x, c2y, seg.end)
        elif isinstance(seg, Arc):
            for cb in seg.as_cubic_curves():
                add_cubic(cb.control1.x, cb.control1.y, cb.control2.x, cb.control2.y, cb.end)
    flush()

    # AE draws the closing segment between the last and first vertex
    # implicitly when closed=True; drop a duplicate final vertex that lands
    # back on the start point so that segment isn't zero-length.
    for sp in subpaths:
        if sp['closed'] and len(sp['vertices']) > 1:
            fx, fy = sp['vertices'][0]
            lx, ly = sp['vertices'][-1]
            if abs(fx - lx) < 1e-4 and abs(fy - ly) < 1e-4:
                sp['inTangents'][0] = sp['inTangents'].pop()
                sp['vertices'].pop()
                sp['outTangents'].pop()
    return [sp for sp in subpaths if len(sp['vertices']) > 1]


def svg_color(c):
    # svgelements' lazy style resolution can hand back a Color object with
    # unset (None) channels for "no color" instead of Python None, depending
    # on what other properties were accessed first on the same element —
    # treat both as "no color".
    if c is None or c.red is None:
        return None
    return {'rgb': [c.red / 255.0, c.green / 255.0, c.blue / 255.0],
            'opacity': c.opacity if c.opacity is not None else 1.0}


def _parse_frac(s, default=0.0):
    """Parses an SVG length that may be a percentage ('50%') or plain number."""
    if s is None:
        return default
    s = s.strip()
    if s.endswith('%'):
        return float(s[:-1]) / 100.0
    return float(s)


def parse_svg_gradients(svg_path):
    """Returns {id: gradient-def} parsed from the raw SVG XML. svgelements
    collapses gradient fills to a flat fallback color and exposes no stop
    data, so gradient stops/geometry have to come from a separate raw pass.

    Only objectBoundingBox gradients without a gradientTransform are marked
    'supported' — userSpaceOnUse would need the shape's cumulative transform
    (which svgelements doesn't expose after resolving geometry), so those
    fall back to a flat color rather than risk placing the ramp wrong."""
    import xml.etree.ElementTree as ET
    from svgelements import Color

    NS = '{http://www.w3.org/2000/svg}'
    XLINK = '{http://www.w3.org/1999/xlink}href'
    root = ET.parse(svg_path).getroot()

    defs = {}
    for el in root.iter():
        tag = el.tag.replace(NS, '')
        if tag in ('linearGradient', 'radialGradient'):
            defs[el.get('id')] = (tag, el)

    def stop_color(stop_el):
        style = {}
        for part in stop_el.get('style', '').split(';'):
            if ':' in part:
                k, v = part.split(':', 1)
                style[k.strip()] = v.strip()
        color_str   = style.get('stop-color')   or stop_el.get('stop-color', '#000000')
        opacity_str = style.get('stop-opacity') or stop_el.get('stop-opacity', '1')
        try:
            c = Color(color_str)
            rgb = (c.red / 255.0, c.green / 255.0, c.blue / 255.0)
        except Exception:
            rgb = (0.0, 0.0, 0.0)
        try:
            opacity = float(opacity_str)
        except ValueError:
            opacity = 1.0
        return rgb, opacity

    def resolve(gid, seen):
        if gid in seen or gid not in defs:
            return None
        seen.add(gid)
        tag, el = defs[gid]

        stops = [
            (_parse_frac(stop.get('offset'), 0.0), *stop_color(stop))
            for stop in el if stop.tag.replace(NS, '') == 'stop'
        ]
        href = el.get('href') or el.get(XLINK)
        if not stops and href:
            parent = resolve(href.lstrip('#'), seen)
            if parent:
                stops = parent['stops']
        if not stops:
            return None

        units = el.get('gradientUnits', 'objectBoundingBox')
        supported = units == 'objectBoundingBox' and not el.get('gradientTransform')

        if tag == 'linearGradient':
            coords = {'x1': _parse_frac(el.get('x1'), 0.0), 'y1': _parse_frac(el.get('y1'), 0.0),
                      'x2': _parse_frac(el.get('x2'), 1.0), 'y2': _parse_frac(el.get('y2'), 0.0)}
            gtype = 'linear'
        else:
            coords = {'cx': _parse_frac(el.get('cx'), 0.5), 'cy': _parse_frac(el.get('cy'), 0.5),
                      'r':  _parse_frac(el.get('r'), 0.5)}
            gtype = 'radial'

        return {'type': gtype, 'stops': stops, 'coords': coords, 'supported': supported}

    return {gid: resolve(gid, set()) for gid in defs}


def shape_gradient_ref(el, gradients, attr):
    """Looks up the raw (pre-resolution) fill/stroke attribute for a
    url(#id) reference — el.fill/el.stroke would already be a flat fallback
    color by this point, so the raw string is read from el.values instead.
    Returns (gradient-def-or-None, had_url_ref) so callers can tell "no
    reference at all" apart from "referenced something we can't use
    (pattern, unresolvable gradient)" — both fall back to a flat color, but
    only the latter should warn."""
    import re
    raw_val = (getattr(el, 'values', None) or {}).get(attr, '')
    m = re.match(r'url\(#([^)]+)\)', raw_val.strip()) if raw_val else None
    if not m:
        return None, False
    return gradients.get(m.group(1)), True


def parse_svg_for_ae(svg_path):
    """Returns (width, height, shapes, warnings). Flat fills/strokes plus
    linear/radial gradients (objectBoundingBox only) — patterns, filters,
    images and text aren't supported yet."""
    from svgelements import SVG, Shape

    warnings = []
    svg = SVG.parse(svg_path)
    width  = int(round(svg.width or 500))
    height = int(round(svg.height or 500))
    gradients = parse_svg_gradients(svg_path)
    unsupported_gradients = 0

    skipped_text = 0
    shapes = []
    for el in svg.elements():
        if not isinstance(el, Shape):
            if type(el).__name__ == 'Text':
                skipped_text += 1
            continue
        subpaths = svg_shape_subpaths(el)
        if not subpaths:
            continue
        opacity = getattr(el, 'opacity', None)
        opacity = opacity if isinstance(opacity, (int, float)) else 1.0

        fill_grad,   fill_had_ref   = shape_gradient_ref(el, gradients, 'fill')
        stroke_grad, stroke_had_ref = shape_gradient_ref(el, gradients, 'stroke')
        for g, had_ref in ((fill_grad, fill_had_ref), (stroke_grad, stroke_had_ref)):
            if had_ref and (g is None or not g['supported']):
                unsupported_gradients += 1

        shapes.append({
            'name':          getattr(el, 'id', None) or f'Shape {len(shapes) + 1}',
            'subpaths':      subpaths,
            'bbox':          el.bbox(),
            'fill':          None if (fill_grad and fill_grad['supported']) else svg_color(el.fill),
            'stroke':        None if (stroke_grad and stroke_grad['supported']) else svg_color(el.stroke),
            'fill_gradient':   fill_grad   if (fill_grad   and fill_grad['supported'])   else None,
            'stroke_gradient': stroke_grad if (stroke_grad and stroke_grad['supported']) else None,
            'stroke_width':  float(el.stroke_width or 1.0),
            'opacity':       opacity,
        })

    if unsupported_gradients:
        warnings.append(f'{unsupported_gradients} shape(s) use gradients with an unsupported '
                         f'coordinate system (userSpaceOnUse/gradientTransform) or patterns — '
                         f'flat colors were used instead.')

    if skipped_text:
        warnings.append(f"{skipped_text} text element(s) skipped — text isn't "
                         f"supported yet; convert text to outlines in your SVG editor first.")

    return width, height, shapes, warnings


def jsx_gradient_ramp(var, grad, bbox, opacity_mult):
    """Emits the AE gradient geometry + color-ramp calls for a fill or
    stroke's "ADBE Vector Graphic - G-Fill"/"G-Stroke" property. Radial
    radius uses the (width+height)/2 approximation for objectBoundingBox
    scaling rather than the exact SVG diagonal-normalization formula — a
    known simplification, close enough for typical near-square shapes."""
    minx, miny, maxx, maxy = bbox
    w, h = maxx - minx, maxy - miny
    c = grad['coords']

    if grad['type'] == 'linear':
        sx, sy = minx + c['x1'] * w, miny + c['y1'] * h
        ex, ey = minx + c['x2'] * w, miny + c['y2'] * h
        grad_type = 1
    else:
        sx, sy = minx + c['cx'] * w, miny + c['cy'] * h
        r_abs = c['r'] * (w + h) / 2.0
        ex, ey = sx + r_abs, sy
        grad_type = 2

    colors_flat, opacities_flat = [], []
    for offset, (r, g, b), stop_op in grad['stops']:
        colors_flat    += [offset, round(r, 4), round(g, 4), round(b, 4)]
        opacities_flat += [offset, round(stop_op * opacity_mult, 4)]

    return [
        f'    {var}.property("ADBE Vector Grad Type").setValue({grad_type});',
        f'    {var}.property("ADBE Vector Grad Start Pt").setValue([{sx:.3f}, {sy:.3f}]);',
        f'    {var}.property("ADBE Vector Grad End Pt").setValue([{ex:.3f}, {ey:.3f}]);',
        f'    var {var}Val = {var}.property("ADBE Vector Grad Colors").value;',
        f'    {var}Val.colors.colors = {json.dumps(colors_flat)};',
        f'    {var}Val.colors.opacities = {json.dumps(opacities_flat)};',
        f'    {var}.property("ADBE Vector Grad Colors").setValue({var}Val);',
    ]


def jsx_shape_layer(shape, idx, enable_3d=False):
    name = json.dumps(shape['name'])
    lines = [
        '  try {',
        f'    var layer{idx} = comp.layers.addShape();',
        f'    layer{idx}.name = {name};',
    ]
    if enable_3d:
        lines.append(f'    layer{idx}.threeDLayer = true;')
    lines += [
        f'    var contents{idx} = layer{idx}.property("ADBE Root Vectors Group");',
        f'    var group{idx} = contents{idx}.addProperty("ADBE Vector Group");',
        f'    group{idx}.name = {name};',
        f'    var groupContents{idx} = group{idx}.property("ADBE Vectors Group");',
    ]

    for si, sp in enumerate(shape['subpaths']):
        lines += [
            f'    var pathProp{idx}_{si} = groupContents{idx}.addProperty("ADBE Vector Shape - Group");',
            f'    var shapeVal{idx}_{si} = pathProp{idx}_{si}.property("ADBE Vector Shape").value;',
            f'    shapeVal{idx}_{si}.vertices = {json.dumps(sp["vertices"])};',
            f'    shapeVal{idx}_{si}.inTangents = {json.dumps(sp["inTangents"])};',
            f'    shapeVal{idx}_{si}.outTangents = {json.dumps(sp["outTangents"])};',
            f'    shapeVal{idx}_{si}.closed = {"true" if sp["closed"] else "false"};',
            f'    pathProp{idx}_{si}.property("ADBE Vector Shape").setValue(shapeVal{idx}_{si});',
        ]

    if shape.get('fill_gradient'):
        var = f'fill{idx}'
        lines.append(f'    var {var} = groupContents{idx}.addProperty("ADBE Vector Graphic - G-Fill");')
        lines += jsx_gradient_ramp(var, shape['fill_gradient'], shape['bbox'], shape['opacity'])
    elif shape['fill']:
        r, g, b = shape['fill']['rgb']
        op = shape['fill']['opacity'] * shape['opacity'] * 100
        lines += [
            f'    var fill{idx} = groupContents{idx}.addProperty("ADBE Vector Graphic - Fill");',
            f'    fill{idx}.property("ADBE Vector Fill Color").setValue([{r:.4f}, {g:.4f}, {b:.4f}]);',
            f'    fill{idx}.property("ADBE Vector Fill Opacity").setValue({op:.2f});',
        ]

    if shape.get('stroke_gradient'):
        var = f'stroke{idx}'
        lines.append(f'    var {var} = groupContents{idx}.addProperty("ADBE Vector Graphic - G-Stroke");')
        lines.append(f'    {var}.property("ADBE Vector Stroke Width").setValue({shape["stroke_width"]:.3f});')
        lines += jsx_gradient_ramp(var, shape['stroke_gradient'], shape['bbox'], shape['opacity'])
    elif shape['stroke']:
        r, g, b = shape['stroke']['rgb']
        op = shape['stroke']['opacity'] * shape['opacity'] * 100
        lines += [
            f'    var stroke{idx} = groupContents{idx}.addProperty("ADBE Vector Graphic - Stroke");',
            f'    stroke{idx}.property("ADBE Vector Stroke Color").setValue([{r:.4f}, {g:.4f}, {b:.4f}]);',
            f'    stroke{idx}.property("ADBE Vector Stroke Width").setValue({shape["stroke_width"]:.3f});',
            f'    stroke{idx}.property("ADBE Vector Stroke Opacity").setValue({op:.2f});',
        ]

    lines += [
        '  } catch (e) {',
        f'    failedShapes.push({name} + ": " + e.toString());',
        '  }',
    ]
    return '\n'.join(lines)


def generate_ae_jsx(comp_name, width, height, shapes, warnings, source_filename, enable_3d=False):
    width  = max(4, int(width))
    height = max(4, int(height))
    body = '\n\n'.join(jsx_shape_layer(s, i, enable_3d) for i, s in enumerate(shapes))
    warn_lines = '\n'.join(f'// NOTE: {w}' for w in warnings)
    comp_name_js = json.dumps(comp_name)

    return f'''// Generated by EditOps — SVG to After Effects
// Source: {source_filename}
// Shapes converted: {len(shapes)}
{warn_lines}
//
// Run this inside After Effects via File > Scripts > Run Script File...
// It builds a composition from the SVG's flat-fill/stroke shapes and
// prompts you to choose where to save the .aep project.

(function() {{
  app.beginUndoGroup("SVG to AE Import");

  var comp = app.project.items.addComp({comp_name_js}, {width}, {height}, 1, 5, 30);
  var failedShapes = [];

{body}

  app.endUndoGroup();

  var msg = "Built \\"" + {comp_name_js} + "\\" with {len(shapes)} shape layer(s).";
  if (failedShapes.length > 0) {{
    msg += "\\n\\n" + failedShapes.length + " shape(s) failed:\\n" + failedShapes.join("\\n");
  }}
  alert(msg);

  var saveFile = File.saveDialog("Save your After Effects project", "*.aep");
  if (saveFile) {{
    app.project.save(saveFile);
    alert("Saved: " + saveFile.fsName);
  }} else {{
    alert("Project built but not saved — use File > Save As to save it later.");
  }}
}})();
'''


@app.route('/svg2aep', methods=['POST'])
def svg2aep_route():
    file = request.files.get('svg')
    if not file:
        return jsonify(error='No SVG file uploaded'), 400

    try:
        import svgelements  # noqa: F401
    except ImportError:
        return jsonify(error='Missing dependency: svgelements. Restart EditOps to pick up '
                              'the update, or run: pip install -r requirements.txt'), 500

    input_path, uid = save_upload(file, fallback_ext='.svg')

    try:
        width, height, shapes, warnings = parse_svg_for_ae(input_path)
    except Exception as e:
        os.remove(input_path)
        return jsonify(error=f'Could not parse this SVG: {e}'), 400

    cleanup_later(input_path)

    if not shapes:
        return jsonify(error='No supported shapes found in this SVG '
                              '(flat fills/strokes only in this version).'), 400

    enable_3d = request.form.get('enable3d') == '1'
    comp_name = stem(file.filename)
    jsx = generate_ae_jsx(comp_name, width, height, shapes, warnings, file.filename, enable_3d)

    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.jsx')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(jsx)

    cleanup_later(output_path)
    resp = send_file(output_path, as_attachment=True,
                     download_name=f'{comp_name}_import.jsx', mimetype='text/plain')
    resp.headers['X-Shape-Count'] = str(len(shapes))
    resp.headers['X-Warnings'] = urllib.parse.quote(json.dumps(warnings))
    return resp


@app.route('/feedback', methods=['POST'])
def feedback_submit():
    data = request.get_json(silent=True) or request.form
    fb_type = (data.get('type') or 'bug').strip()
    message = (data.get('message') or '').strip()
    name    = (data.get('name') or '').strip()

    if fb_type not in ('bug', 'idea'):
        fb_type = 'bug'
    if not message:
        return jsonify(error='Please enter a description.'), 400

    try:
        supabase_request('POST', 'feedback', {'type': fb_type, 'message': message, 'name': name})
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    return jsonify(ok=True)


@app.route('/feedback/list')
def feedback_list():
    try:
        rows = supabase_request('GET', 'feedback?select=*&order=id.desc')
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    return jsonify(rows)


@app.route('/feedback/<int:fb_id>/status', methods=['POST'])
def feedback_set_status(fb_id):
    data = request.get_json(silent=True) or request.form
    status = data.get('status')
    if status not in ('open', 'resolved'):
        return jsonify(error='Invalid status'), 400

    try:
        supabase_request('PATCH', f'feedback?id=eq.{fb_id}', {'status': status})
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    return jsonify(ok=True)


@app.route('/feedback/<int:fb_id>/delete', methods=['POST'])
def feedback_delete(fb_id):
    try:
        supabase_request('DELETE', f'feedback?id=eq.{fb_id}')
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    return jsonify(ok=True)


@app.route('/ytdl', methods=['POST'])
def ytdl_route():
    url = request.form.get('url', '').strip()
    quality = request.form.get('quality', '720')

    if not url:
        return jsonify(error='Please provide a YouTube URL.'), 400

    uid = str(uuid.uuid4())
    out_tmpl = os.path.join(TEMP_DIR, f'vt_yt_{uid}.%(ext)s')

    # Invoke yt-dlp as a module of the running interpreter rather than a bare
    # command — on Windows the launcher never activates the venv, so a plain
    # 'yt-dlp' on PATH would not resolve to venv\Scripts\yt-dlp.exe.
    ytdlp_base = [sys.executable, '-m', 'yt_dlp']

    if quality == 'audio':
        cmd = [*ytdlp_base, '-x', '--audio-format', 'mp3', '--audio-quality', '0',
               '-N', '4',
               '--no-playlist', '--print', '%(title)s', '--no-simulate',
               '-o', out_tmpl, url]
    else:
        # Prefer H.264+M4A: fastest merge, widest compatibility, no re-encode needed
        fmt = (
            f'bestvideo[height<={quality}][vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]'
            f'/bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]'
            f'/bestvideo[height<={quality}]+bestaudio'
            f'/best[height<={quality}]'
        )
        cmd = [*ytdlp_base, '-f', fmt, '--merge-output-format', 'mp4',
               '-N', '4',
               '--no-playlist', '--print', '%(title)s', '--no-simulate',
               '-o', out_tmpl, url]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if r.returncode != 0:
        err = r.stderr[-300:] if r.stderr else 'Unknown error'
        return jsonify(error=f'Download failed. {err}'), 500

    # Find the downloaded file
    matches = glob.glob(os.path.join(TEMP_DIR, f'vt_yt_{uid}.*'))
    if not matches:
        return jsonify(error='Downloaded file not found.'), 500

    output_path = matches[0]
    ext = os.path.splitext(output_path)[1]

    # Title comes from --print %(title)s in stdout (first non-empty line)
    raw_title = next((l for l in r.stdout.splitlines() if l.strip()), 'video')
    safe_title = ''.join(c for c in raw_title if c.isalnum() or c in ' -_').strip()[:80]
    download_name = f'{safe_title}{ext}' if safe_title else f'video{ext}'

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True, download_name=download_name)


# ── On-Screen Spelling QA ───────────────────────────────────────────────────
# Catches spelling mistakes burned into video pixels (lower thirds, titles,
# graphics) — text that only exists as pixels, not as any extractable data,
# so it has to go through OCR before it can be spellchecked at all.

_ocr_reader = None
_ocr_reader_lock = threading.Lock()


def get_ocr_reader():
    """Loads the EasyOCR model once and reuses it — model init is a couple
    seconds, worth caching across requests rather than reloading every scan."""
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_reader_lock:
            if _ocr_reader is None:
                import easyocr
                _ocr_reader = easyocr.Reader(['en'], gpu=True, verbose=False)
    return _ocr_reader


def qa_sample_interval(duration, max_frames=90, min_interval=1.5):
    """Fixed-interval frame sampling. Scene-change detection was tried first
    but doesn't reliably catch this case: on-screen text over a similar
    background (the common lower-third/title-card case) barely moves the
    scene-change score even though the words are completely different —
    tuning the threshold low enough to catch it also fires constantly on
    ordinary camera motion in real footage. Fixed interval is simpler and
    doesn't have that blind spot."""
    if duration <= 0:
        return min_interval
    return max(min_interval, duration / max_frames)


def qa_extract_frames(video_path, out_dir, interval):
    """One ffmpeg pass, sampling at a fixed interval and downscaling to
    bound OCR time on large source video. Returns [(timestamp, frame_path)]."""
    pattern = os.path.join(out_dir, 'f%05d.jpg')
    fps = 1.0 / interval
    cmd = ['ffmpeg', '-y', '-i', video_path,
           '-vf', f"fps={fps},scale='min(960,iw)':-2",
           '-q:v', '4', pattern]
    subprocess.run(cmd, capture_output=True)
    frames = sorted(glob.glob(os.path.join(out_dir, 'f*.jpg')))
    return [(i * interval, path) for i, path in enumerate(frames)]


def qa_crop_thumbnail(frame_path, bbox, pad_frac=0.6, max_width=480):
    """Crops the frame to the flagged word's region (with padding for visual
    context) and returns it as a base64 JPEG data URI — OCR misreads are
    common enough on stylized text that a bare word list isn't trustworthy
    on its own; the reviewer needs to glance and confirm."""
    from PIL import Image
    import base64, io as pyio

    img = Image.open(frame_path)
    w_img, h_img = img.size
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x0, x1 = float(min(xs)), float(max(xs))
    y0, y1 = float(min(ys)), float(max(ys))
    padx = (x1 - x0) * pad_frac
    pady = (y1 - y0) * pad_frac + 10
    cx0 = max(0, int(x0 - padx))
    cx1 = min(w_img, int(x1 + padx))
    cy0 = max(0, int(y0 - pady))
    cy1 = min(h_img, int(y1 + pady))
    crop = img.crop((cx0, cy0, cx1, cy1))
    if crop.width > max_width:
        ratio = max_width / crop.width
        crop = crop.resize((max_width, max(1, int(crop.height * ratio))))

    buf = pyio.BytesIO()
    crop.convert('RGB').save(buf, format='JPEG', quality=80)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


# Common Hinglish/Hindi words (romanized) that an English dictionary always
# flags as misspelled — excluded so Hindi/Hinglish on-screen text doesn't
# drown out genuine English typos. A starter list, not exhaustive; extend
# as real false positives turn up.
HINGLISH_WORDS = frozenset({
    'aap', 'aapka', 'aapke', 'aapki', 'aapko', 'hai', 'hain', 'ho', 'hoga',
    'hogi', 'hote', 'hoti', 'hota', 'kar', 'karo', 'kare', 'karen', 'karein',
    'karenge', 'kiya', 'kiye', 'kya', 'kaise', 'kab', 'kahan', 'kaun', 'kyun',
    'kyu', 'kyunki', 'nahi', 'nahin', 'haan', 'han', 'ji', 'bhai', 'bhaiya',
    'didi', 'yaar', 'dost', 'accha', 'achha', 'acha', 'theek', 'thik',
    'matlab', 'bilkul', 'zaroor', 'zarur', 'jarur', 'dekho', 'dekhiye',
    'dekhna', 'suniye', 'sunna', 'batao', 'bataiye', 'bataunga', 'chalo',
    'chaliye', 'abhi', 'phir', 'fir', 'iske', 'uske', 'isme', 'usme',
    'jaise', 'waise', 'aisa', 'waisa', 'kuch', 'kuchh', 'sabhi', 'sab',
    'hum', 'humein', 'humara', 'humare', 'tumhara', 'tumhare', 'mera',
    'mere', 'meri', 'tera', 'teri', 'uska', 'uski', 'unka', 'unki', 'wala',
    'wali', 'wale', 'log', 'logo', 'logon', 'baat', 'cheez', 'chize',
    'zindagi', 'duniya', 'paisa', 'paise', 'zaroori', 'jarurat', 'zarurat',
    'namaste', 'shukriya', 'dhanyavad', 'aage', 'peeche', 'andar', 'bahar',
    'upar', 'neeche', 'niche', 'idhar', 'udhar', 'yahan', 'wahan', 'bohot',
    'bahut', 'thoda', 'zyada', 'jyada', 'zyaada', 'kam', 'aur', 'toh', 'bhi',
    'mein', 'sahi', 'galat', 'ekdum', 'sasta', 'mehnga', 'mehenga', 'kaafi',
    'kafi', 'pyaar', 'pyar', 'dil', 'insaan', 'samay', 'waqt',
})

# Finance/investment jargon and acronyms that are legitimate but not in a
# general English dictionary (confirmed missing via direct lookup, e.g.
# "underperforming" — present in real client content, unlike "outperforming"
# which the dictionary does know). A starter list, not exhaustive; extend
# as real false positives turn up, same as HINGLISH_WORDS.
FINANCE_JARGON_WORDS = frozenset({
    'sebi', 'rbi', 'irdai', 'amc', 'amfi', 'nav', 'sip', 'sips', 'elss',
    'etf', 'etfs', 'ipo', 'ipos', 'nfo', 'nfos', 'kyc', 'cagr', 'aum',
    'ltcg', 'stcg', 'ulip', 'ulips', 'npa', 'npas', 'nifty', 'sensex',
    'bse', 'nse', 'fintech', 'demat', 'folio', 'lumpsum', 'largecap',
    'midcap', 'smallcap', 'multicap', 'flexicap', 'fmcg', 'pharma',
    'underperforming', 'underperform', 'underperformance', 'overperform',
    'rebalancing', 'rebalance', 'derivatives', 'equities', 'fincard',
})


def qa_get_dismissed_words():
    """Fetches the team's accumulated verdicts from Supabase and returns the
    set of words currently marked "not a mistake" — the most recent verdict
    per word wins, so a word that was dismissed and later re-confirmed as a
    real mistake stops being suppressed. Returns an empty set on any error
    (network down, table missing) rather than failing the whole scan."""
    try:
        rows = supabase_request('GET', 'qa_word_feedback?select=word,verdict,created_at&order=created_at.desc')
    except Exception:
        return set()
    if not rows:
        return set()
    latest = {}
    for row in rows:
        word = row['word']
        if word not in latest:
            latest[word] = row['verdict']
    return {w for w, v in latest.items() if v == 'not_mistake'}


def qa_is_merged_words(word, spell, min_total_len=9, min_part_len=2, max_part_len=15):
    """True if `word` can be fully segmented into 2+ real dictionary words
    with no leftover characters — a strong signal OCR merged separate
    words together (missed a space) rather than this being a genuine
    typo, e.g. "understandyour" -> "understand" + "your".

    Gated by min_total_len so short words are never affected: a real typo
    can coincidentally look like two short words mashed together
    ("wellcome" = "well" + "come", one of this app's own confirmed real
    typos), but that risk drops sharply as length grows — both because
    longer merges increasingly require 3+ exact-boundary matches to fully
    segment (matching one wrong dictionary word by luck is plausible,
    matching a whole chain of them at the exact right cut points isn't),
    and because requiring a *complete* segmentation with nothing left over
    is a much stricter bar than any single substring happening to be a
    real word."""
    word = word.lower()
    n = len(word)
    if n < min_total_len:
        return False

    min_parts = [None] * (n + 1)  # fewest dictionary words to reach prefix of length i
    min_parts[0] = 0
    for i in range(1, n + 1):
        for j in range(max(0, i - max_part_len), i - min_part_len + 1):
            if min_parts[j] is None:
                continue
            if spell.known([word[j:i]]):
                if min_parts[i] is None or min_parts[j] + 1 < min_parts[i]:
                    min_parts[i] = min_parts[j] + 1

    return min_parts[n] is not None and min_parts[n] >= 2


def qa_check_spelling(segments, dismissed_words=frozenset(), max_unknown_ratio=0.5, min_words_for_ratio=4):
    """Per-word English spellcheck across segments, but skips a whole line when most of its words
    aren't recognized by the English dictionary — much more likely a
    non-English (e.g. Hinglish) sentence than one riddled with typos, so
    flagging individual words in it would mostly be noise. HINGLISH_WORDS
    remains a second layer for isolated Hinglish words mixed into an
    otherwise-English line, which the ratio check alone wouldn't catch.
    dismissed_words is the team's own accumulated "not a mistake" verdicts
    from qa_get_dismissed_words — the same idea as HINGLISH_WORDS, except
    it grows itself from real usage instead of being hand-curated.

    The ratio only applies once there are enough checkable words to make it
    a meaningful signal — a short title with two typos ("Wellcome to the
    shwo") can easily hit a >50% unknown ratio on its own merits without
    being remotely non-English, so lines below min_words_for_ratio always
    fall back to flagging each unknown word individually."""
    try:
        from spellchecker import SpellChecker
        import re
        spell = SpellChecker()
        # A bare "@" alone is included (not just full email patterns) since
        # OCR frequently drops the period in domains ("service@tataamccom"
        # instead of "service@tataamc.com") — ordinary prose essentially
        # never contains "@", so its presence alone is a strong enough signal.
        # \bwww[a-z] (no dot required) catches OCR merging "www" straight
        # into the domain ("wwwicicidirect" instead of "www.icicidirect"),
        # general across any client's domain rather than one hardcoded name.
        # "tatamutualfund" stays as a specific catch for a related but
        # different corruption — OCR sometimes drops one or two of the w's
        # too ("wtatamutualfund"/"wwtatamutualfund"), which won't match
        # \bwww[a-z] since it no longer starts with a full "www".
        # \bhttps?\b alone (not just "https://") since OCR sometimes mangles
        # the slashes into something else entirely ("https:|" instead of
        # "https://") — a bare "http"/"https" token is still an unambiguous
        # signal the line is a URL fragment, whatever follows it.
        url_pattern = re.compile(
            r'https?://|\bhttps?\b|www\.|\bwww[a-z]|@|\.(com|in|org|net|co)\b|tatamutualfund',
            re.IGNORECASE)
        issues = []
        for i, seg in enumerate(segments):
            if url_pattern.search(seg['text']):
                continue  # URLs/emails word-split into garbage tokens, never real prose
            words = re.findall(r"[A-Za-z']+", seg['text'])
            to_check = [w.lower().strip("'") for w in words
                        if len(w) > 2 and not w.isupper()]
            if not to_check:
                continue
            unknown = spell.unknown(to_check)
            if len(to_check) >= min_words_for_ratio and len(unknown) / len(to_check) > max_unknown_ratio:
                continue
            for word in unknown:
                if word in HINGLISH_WORDS or word in FINANCE_JARGON_WORDS or word in dismissed_words:
                    continue
                if qa_is_merged_words(word, spell):
                    continue
                best = spell.correction(word)
                others = sorted(spell.candidates(word) or set())
                suggestions = ([best] if best and best != word else []) + \
                              [c for c in others if c != word and c != best]
                issues.append({
                    'seg_idx':     i,
                    'word':        word,
                    'suggestions': suggestions[:3],
                    'start':       seg['start'],
                })
        return issues
    except Exception:
        return []


def qa_y_bucket(y_mid, frame_h):
    """5%-of-frame-height buckets — coarse enough to absorb OCR bbox jitter
    (a line with descenders like g/y reads a slightly taller box than one
    without) while still being far more precise than a fixed percentage."""
    return round(y_mid / frame_h * 20) / 20


def qa_detect_recurring_bands(raw_results, frames, min_run_frac=0.2):
    """Auto-detects Y-positions in the bottom half of frame that hold a
    single, mostly-unbroken template element — a subtitle track, or a
    continuously-shown disclaimer — instead of assuming a fixed percentage
    or judging by text size.

    Deliberately not size-based: in real content, a compact infographic's
    small labels can be physically smaller than a disclaimer line, so
    filtering by height would hide legitimate (and possibly misspelled)
    graphic text right along with the disclaimer — proven concretely
    against a real sample video where a confirmed typo in a small graphic
    label measured smaller than that video's own disclaimer text.

    Also deliberately not just "total frequency in this band", which
    turned out to have the same kind of failure mode from a different
    angle: on a video with several distinct full-screen graphics/UI
    screenshots, unrelated one-off content from completely different
    points in the video can coincidentally land in the same Y-band often
    enough to clear a frequency threshold, even though none of them are
    individually recurring — confirmed concretely on a real sample video,
    where a genuine disclaimer band (present in a single unbroken 26-frame
    stretch) had *lower* total frequency than a band that was actually a
    coincidental mix of five unrelated screens (max unbroken stretch: 12
    frames), meaning no frequency cutoff could separate them correctly.

    The signal that did cleanly separate them: longest *unbroken* run of
    consecutive sampled frames. A real template element persists across a
    continuous stretch; unrelated screens sharing a Y-band by coincidence
    show up as several short, scattered bursts instead. Qualifies a band
    if its longest unbroken run is at least min_run_frac of all sampled
    frames. Empty set if nothing clearly qualifies (safer to risk missing
    one than to blindly exclude real graphic text)."""
    bucket_frames = {}
    for ts, _frame_path, bbox, _text, frame_h in raw_results:
        y_mid = sum(p[1] for p in bbox) / len(bbox)
        if y_mid < frame_h * 0.6:
            continue
        b = qa_y_bucket(y_mid, frame_h)
        bucket_frames.setdefault(b, set()).add(ts)

    total_frames = len(frames)
    if total_frames < 2:
        return set()
    interval = frames[1][0] - frames[0][0]

    qualifying = set()
    for b, ts_set in bucket_frames.items():
        ts_sorted = sorted(ts_set)
        longest = cur = 1
        for i in range(1, len(ts_sorted)):
            if abs(ts_sorted[i] - ts_sorted[i - 1] - interval) < interval * 0.1:
                cur += 1
            else:
                cur = 1
            longest = max(longest, cur)
        if longest / total_frames >= min_run_frac:
            qualifying.add(b)
    return qualifying


def qa_dedupe_issues(issues, window=5.0):
    """Collapses the same flagged word appearing across several consecutive
    sampled frames (a lower third held on screen for a few seconds gets
    sampled multiple times) down to its first occurrence within a rolling
    time window, so review isn't cluttered with near-duplicates."""
    issues = sorted(issues, key=lambda i: i['start'])
    kept = []
    last_seen = {}
    for iss in issues:
        prev_t = last_seen.get(iss['word'])
        if prev_t is not None and iss['start'] - prev_t < window:
            continue
        kept.append(iss)
        last_seen[iss['word']] = iss['start']
    return kept


def qa_scan_frames(frames, progress_cb=None):
    """Runs OCR on each sampled frame, auto-detects and excludes this
    video's recurring bands (subtitles, continuously-shown disclaimers),
    spellchecks what's left (skipping likely-non-English lines — see
    qa_check_spelling), and returns deduped
    issues with cropped thumbnails."""
    from PIL import Image

    reader = get_ocr_reader()

    # Pass 1: OCR every frame and keep all raw results — the subtitle band
    # can't be identified until we've seen where text recurs across the
    # whole video, so nothing gets excluded yet.
    raw_results = []  # (ts, frame_path, bbox, text, frame_h)
    for i, (ts, frame_path) in enumerate(frames):
        if progress_cb:
            progress_cb(i, len(frames))
        frame_h = Image.open(frame_path).size[1]
        for bbox, text, conf in reader.readtext(frame_path):
            if conf < 0.4:
                continue
            raw_results.append((ts, frame_path, bbox, text, frame_h))

    recurring_bands = qa_detect_recurring_bands(raw_results, frames)

    ocr_segments = []   # [{'text', 'start'}] — qa_check_spelling's expected shape
    frame_meta   = []   # parallel to ocr_segments: (frame_path, bbox)
    for ts, frame_path, bbox, text, frame_h in raw_results:
        if recurring_bands:
            y_mid = sum(p[1] for p in bbox) / len(bbox)
            if qa_y_bucket(y_mid, frame_h) in recurring_bands:
                continue
        ocr_segments.append({'text': text, 'start': ts})
        frame_meta.append((frame_path, bbox))

    dismissed_words = qa_get_dismissed_words()
    raw_issues = qa_check_spelling(ocr_segments, dismissed_words)

    issues = []
    for iss in raw_issues:
        frame_path, bbox = frame_meta[iss['seg_idx']]
        issues.append({
            'word':        iss['word'],
            'suggestions': iss['suggestions'],
            'start':       iss['start'],
            'context':     ocr_segments[iss['seg_idx']]['text'],
            'thumbnail':   qa_crop_thumbnail(frame_path, bbox),
        })

    return qa_dedupe_issues(issues)


@app.route('/qacheck', methods=['POST'])
def qacheck_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No video uploaded'), 400

    try:
        import easyocr  # noqa: F401
    except ImportError:
        return jsonify(error='Missing dependency: easyocr. Restart EditOps to pick up '
                              'the update, or run: pip install -r requirements.txt'), 500

    input_path, uid = save_upload(file)
    info = ffprobe_info(input_path)
    if not info or not info['has_video']:
        os.remove(input_path)
        return jsonify(error='Cannot read video file.'), 400

    _tasks[uid] = {'status': 'processing', 'progress': 'Extracting frames…'}

    def run():
        frame_dir = os.path.join(TEMP_DIR, f'vt_qa_{uid}')
        os.makedirs(frame_dir, exist_ok=True)
        try:
            interval = qa_sample_interval(info['duration'])
            frames = qa_extract_frames(input_path, frame_dir, interval)
            if not frames:
                _tasks[uid] = {'status': 'error', 'error': 'Could not extract frames from this video.'}
                return

            _tasks[uid]['progress'] = f'Scanning {len(frames)} frames… (first run downloads the OCR model)'

            def progress(i, total):
                _tasks[uid]['progress'] = f'Scanning frame {i + 1}/{total}…'

            issues = qa_scan_frames(frames, progress)

            _tasks[uid] = {
                'status':         'done',
                'issues':         issues,
                'frames_scanned': len(frames),
                'duration':       info['duration'],
            }
        except Exception as e:
            _tasks[uid] = {'status': 'error', 'error': str(e)[:300]}
        finally:
            cleanup_later(input_path)
            def cleanup_frame_dir():
                time.sleep(90)
                shutil.rmtree(frame_dir, ignore_errors=True)
            threading.Thread(target=cleanup_frame_dir, daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return jsonify(task_id=uid)


@app.route('/qacheck/status/<task_id>')
def qacheck_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify(error='Task not found'), 404
    return jsonify(task)


QA_DISMISS_REASONS = ('ocr_misread', 'animation', 'not_english', 'other')


@app.route('/qacheck/feedback', methods=['POST'])
def qacheck_feedback_route():
    data = request.get_json(silent=True) or request.form
    word    = (data.get('word') or '').strip().lower()
    verdict = (data.get('verdict') or '').strip()
    reason     = (data.get('reason') or '').strip()
    context    = (data.get('context') or '').strip()
    video_name = (data.get('video_name') or '').strip()

    if not word:
        return jsonify(error='Missing word.'), 400
    if verdict not in ('mistake', 'not_mistake'):
        return jsonify(error='Invalid verdict.'), 400
    if reason and reason not in QA_DISMISS_REASONS:
        return jsonify(error='Invalid reason.'), 400

    try:
        supabase_request('POST', 'qa_word_feedback', {
            'word': word, 'verdict': verdict, 'reason': reason or None,
            'context': context, 'video_name': video_name,
        })
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    return jsonify(ok=True)


@app.route('/qacheck/feedback/list')
def qacheck_feedback_list_route():
    try:
        rows = supabase_request('GET', 'qa_word_feedback?select=*&order=created_at.desc')
    except Exception:
        return jsonify(error='Could not reach the feedback server. Check your internet connection.'), 502
    latest = {}
    for row in rows or []:
        if row['word'] not in latest:
            latest[row['word']] = row
    return jsonify(sorted(latest.values(), key=lambda r: r['word']))


# ── Transcription ────────────────────────────────────────────────────────────

def romanize_text(text):
    """Convert Devanagari characters to Roman (ITRANS). Latin chars pass through unchanged."""
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        return transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        return text


def _gemini_client():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            'Gemini API key not configured. Add GEMINI_API_KEY to a .env '
            'file in the EditOps folder and restart the app.')
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _parse_gemini_json(response):
    """Strip an optional ```json fence and parse the response text as JSON."""
    raw = response.text.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1] if '\n' in raw else raw
        raw = raw.rsplit('```', 1)[0]
    return json.loads(raw)



# ISO 639-1 -> readable name, for the languages offered in the Transcribe
# dropdown. Used so the Gemini prompt reads naturally ("language is Odia")
# instead of interpolating a raw code ("language is or" — which for Odia
# would otherwise literally read as the English word "or" mid-sentence).
LANGUAGE_NAMES = {
    'en': 'English', 'hi': 'Hindi', 'mr': 'Marathi', 'gu': 'Gujarati',
    'ta': 'Tamil', 'te': 'Telugu', 'kn': 'Kannada', 'ml': 'Malayalam',
    'pa': 'Punjabi', 'or': 'Odia', 'es': 'Spanish', 'fr': 'French',
    'de': 'German', 'pt': 'Portuguese', 'ja': 'Japanese', 'zh': 'Chinese',
    'ar': 'Arabic', 'ru': 'Russian', 'ko': 'Korean', 'it': 'Italian',
    'nl': 'Dutch', 'tr': 'Turkish',
}


def gemini_transcribe(wav_path, language=None, romanize=False):
    """Transcribe via the Gemini API. Returns (segments, detected_language).

    Unlike Whisper, Gemini has no native forced-alignment — timestamps are
    the model reading them back off its own transcript, so they're
    reasonably good for short/medium files but can drift on long ones.
    Raises on any failure (missing key, API error, bad response) so the
    caller can surface a clear task error.
    """
    client = _gemini_client()
    uploaded = client.files.upload(file=wav_path)

    # Phrased as "primary" rather than "the" language, with an explicit
    # no-skip instruction — an earlier version ("The spoken language is
    # Hindi.") read as exclusive and could cause the model to drop or
    # garble a sentence that's actually spoken fully in another language
    # (e.g. English) instead of transcribing it as-is.
    lang_hint = (
        f' The primary spoken language is {LANGUAGE_NAMES.get(language, language)}, '
        'but some sentences may be spoken entirely in a different language '
        '(e.g. English) — transcribe those exactly as spoken too. Never '
        'omit, skip, or merge a sentence just because it is in a different '
        'language than the primary one.'
        if language else ''
    )
    script_instruction = (
        ' Any Hindi/Urdu (or other non-Latin-script) speech must be written '
        'in casual Roman-script transliteration the way people actually type '
        'it informally (e.g. "kya kar rahe ho", "nahi pata") — NOT formal '
        'academic transliteration with diacritics or capitalized long vowels. '
        'English speech stays in English as normal.'
        if romanize else
        ' Write each language in its own native script (e.g. Devanagari for '
        'Hindi), not transliterated.'
    )
    # This is the fix for word-level code-switching, not just sentence-level:
    # without it, a common English loanword spoken mid-Hindi-sentence (e.g.
    # "mutual fund", "trip", "hotel") tends to get phonetically
    # transliterated into Devanagari ("म्यूचुअल फंड") instead of kept in its
    # correct English spelling — verified on Video 11.mp4, where every one
    # of ~15 such loanwords across the clip came out wrong without this,
    # and correct with it.
    english_word_instruction = (
        ' This audio code-switches at the word level, not just the '
        'sentence level — a sentence in the primary language will often '
        'contain individual English words or short phrases spoken in '
        'English (e.g. "mutual fund", "trip", "hotel", "agent", "advisor", '
        'the way English loanwords are used casually in everyday Hindi '
        'speech). Identify each such word by ear — if it is genuinely an '
        'English word, even a common one used casually inside a sentence '
        'in another language, write it in its correct English spelling '
        'using Latin letters. Do NOT phonetically transliterate it into '
        'the other script/language just because of the sentence it is '
        'embedded in — only words that are genuinely from that other '
        'language should be written that way.'
    )
    # This model gets the real audio, not just text, so it can identify
    # distinct voices directly — no separate diarization system needed.
    # Verified deterministic (2 repeat runs on the same clip matched
    # exactly and correctly found all 4 speakers on a real clip).
    speaker_instruction = (
        ' Identify each distinct speaker by their voice and include a '
        '"speaker" field on every object naming which speaker is talking '
        '(e.g. "Speaker 1", "Speaker 2"), using the same label for the same '
        'voice consistently throughout, in order of first appearance. Omit '
        'the field (or leave it empty) if you can only detect one speaker.'
    )
    prompt = (
        'Transcribe this audio.' + lang_hint + script_instruction +
        english_word_instruction + speaker_instruction +
        ' Return ONLY a JSON array (no markdown, no commentary) of objects '
        'with keys "start" (seconds, number), "end" (seconds, number), '
        '"speaker" (string, optional), and "text" (string), one per natural '
        'sentence or phrase, covering the entire audio from start to finish '
        'in order.'
    )
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=[uploaded, prompt],
    )
    parsed = _parse_gemini_json(response)

    segs = [
        {
            'start': float(s['start']),
            'end': float(s['end']),
            'text': s['text'].strip(),
            '_speaker': (s.get('speaker') or '').strip() or None,
        }
        for s in parsed
    ]

    # Only label speakers when more than one was actually detected, so a
    # single-speaker video's output looks identical to before this was added.
    speaker_order = []
    for s in segs:
        if s['_speaker'] and s['_speaker'] not in speaker_order:
            speaker_order.append(s['_speaker'])
    if len(speaker_order) > 1:
        for s in segs:
            if s['_speaker']:
                s['text'] = f"{s['_speaker']}: {s['text']}"
    for s in segs:
        s.pop('_speaker', None)

    return segs, (language or '')


def gemini_romanize_segments(segs):
    """Rewrite Devanagari (or other non-Latin) text in `segs` as casual
    Roman-script Hinglish via Gemini, the way people actually type it —
    much closer to natural than the local ITRANS transliteration library,
    which produces technically-correct but unnatural output (capitalized
    long vowels, retained schwas, etc). Returns a new list; raises on
    failure so the caller can decide how to fall back.
    """
    client = _gemini_client()
    prompt = (
        'Here is a JSON array of transcript lines. Any Devanagari (or other '
        'non-Latin-script) text in them should be rewritten as casual '
        'Roman-script transliteration the way people actually type it '
        'informally (e.g. "kya kar rahe ho", "nahi pata") — NOT formal '
        'academic transliteration with diacritics or capitalized long '
        'vowels. Lines already in Latin script should be returned '
        'unchanged. Return ONLY a JSON array of strings, same length and '
        'order as the input, no markdown, no commentary.\n\n' +
        json.dumps([s['text'] for s in segs], ensure_ascii=False)
    )
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=[prompt],
    )
    parsed = _parse_gemini_json(response)
    if len(parsed) != len(segs):
        raise ValueError('Gemini romanize returned a different number of lines than sent.')
    return [{**s, 'text': str(t).strip()} for s, t in zip(segs, parsed)]


def gemini_enforce_keywords_english(segs, keywords):
    """Rewrite any of `keywords` that ended up translated or transliterated
    in `segs` back to their exact English spelling. Runs as a fixup pass
    after transcription, applied the same way regardless of which
    transcription backend produced `segs`. Returns a new list; raises on
    failure so the caller can decide how to fall back.
    """
    client = _gemini_client()
    keyword_list = ', '.join(f'"{k}"' for k in keywords)
    prompt = (
        'Here is a JSON array of transcript lines. The following words/terms '
        f'must always appear exactly as given, in English, never translated '
        f'or transliterated into another script or spelling, no matter what '
        f'language surrounds them: {keyword_list}. Fix any line where one of '
        'these terms was translated or transliterated instead of kept in '
        'English. Leave everything else in each line unchanged, including '
        'lines that don\'t contain any of these terms. Return ONLY a JSON '
        'array of strings, same length and order as the input, no markdown, '
        'no commentary.\n\n' +
        json.dumps([s['text'] for s in segs], ensure_ascii=False)
    )
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=[prompt],
    )
    parsed = _parse_gemini_json(response)
    if len(parsed) != len(segs):
        raise ValueError('Gemini keyword-enforcement returned a different number of lines than sent.')
    return [{**s, 'text': str(t).strip()} for s, t in zip(segs, parsed)]


# ── Translate & Dub ──────────────────────────────────────────────────────────
#
# Pipeline: source video -> (Gemini) transcribe + diarize + translate +
# classify each line's emotional delivery, in one multimodal call, since
# Gemini hears the real audio and can judge tone/energy directly rather than
# guessing from text -> user reviews/edits the translated transcript ->
# once locked, (ElevenLabs) generate speech per line with a voice cloned
# from the original speaker, with an inline Audio Tag driving that line's
# emotional delivery. Lip-syncing the result onto the source video is a
# separate, later step (not built here).

# Mapping from Gemini's emotion classification to an Eleven v3 inline
# Audio Tag (e.g. "[excited]") prepended to the line's text. This replaces
# an earlier version that drove emotion through voice_settings' numeric
# stability/style sliders on the older eleven_multilingual_v2 model — that
# approach was tested against a real client video and sounded bad: low
# stability combined with high style produces unstable, artifact-prone
# delivery, especially on an Instant Voice Clone. v3's audio tags are the
# mechanism ElevenLabs actually built for this, and work with IVC voices.
EMOTION_AUDIO_TAGS = {
    # A blank tag gives the model zero delivery guidance rather than a
    # neutral-but-expressive one, and real testing showed exactly that
    # line coming out flatter/more robotic than every tagged line around
    # it — "[naturally]" still directs the model instead of leaving it
    # with nothing to go on.
    'neutral':     '[naturally]',
    'calm':        '[calmly]',
    'happy':       '[happily]',
    'excited':     '[excited]',
    'urgent':      '[urgently]',
    'sad':         '[sad]',
    'angry':       '[angry]',
    'questioning': '[curious]',
    'reassuring':  '[reassuringly]',
    'sarcastic':   '[sarcastically]',
}


def _elevenlabs_headers():
    if not ELEVENLABS_API_KEY:
        raise ValueError(
            'ElevenLabs API key not configured. Add ELEVENLABS_API_KEY to a .env '
            'file in the project root (get one from https://elevenlabs.io).'
        )
    return {'xi-api-key': ELEVENLABS_API_KEY}


def _elevenlabs_call(req, timeout=60):
    """Run an ElevenLabs request, surfacing the API's own error message
    (in the response body) instead of a bare HTTP status on failure."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        raise ValueError(f'ElevenLabs API error ({e.code}): {body[:300]}') from e


def elevenlabs_clone_voice(sample_paths, name):
    """Create an Instant Voice Clone from one or more short audio samples.
    Returns the new voice_id. Raises on failure.

    Each sample is sent as its own "files" part rather than pre-
    concatenated into one file — ElevenLabs' own guidance is that it
    fuses multiple clean clips itself, and a naive ffmpeg concat (the
    earlier version of this function) introduces its own splice artifact
    at every cut point, which an in isolation-trained clone would
    otherwise pick up as if it were a voice characteristic.

    remove_background_noise=true runs ElevenLabs' own Voice Isolator model
    on the samples before cloning — without it, background music under
    the speaker in the source video gets cloned as part of the "voice"
    itself (echo-y, musical artifacts in the generated speech).
    """
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="remove_background_noise"\r\n\r\ntrue\r\n'.encode(),
    ]
    for i, sample_path in enumerate(sample_paths):
        with open(sample_path, 'rb') as f:
            audio_bytes = f.read()
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="sample_{i}.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + audio_bytes + b'\r\n'
        )
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(parts)

    req = urllib.request.Request(
        'https://api.elevenlabs.io/v1/voices/add',
        data=body,
        method='POST',
        headers={
            **_elevenlabs_headers(),
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
    )
    return json.loads(_elevenlabs_call(req, timeout=120))['voice_id']


def elevenlabs_tts(run_segments, voice_id, out_path):
    """Generate ONE continuous speech clip on Eleven v3 for `run_segments`
    — a list of {text, emotion} dicts, in order, all from the same
    speaker's uninterrupted turn. Each segment's own Audio Tag (e.g.
    "[excited]") is placed inline ahead of its text, so delivery can still
    shift line to line, but the whole passage is one generation.

    This matters because separate API calls have no memory of each other
    — stitching many short per-sentence clips together is what produced
    disjointed, part-by-part-sounding delivery instead of one flowing
    performance, verified against real client feedback. ElevenLabs' own
    fix for this, Request Stitching (previous_request_ids), is explicitly
    unsupported on eleven_v3 (confirmed against their docs) — the model
    this pipeline uses for Audio Tags — so merging same-speaker segments
    into fewer, longer calls is the only way to get continuity while
    keeping v3's emotion control. A speaker change still needs its own
    call regardless, since it needs a different cloned voice.

    `stability` is fixed at 0.5 ("Natural" — v3 only takes 0/0.5/1.0, not
    a continuous slider like v2) so it stays responsive to the tags
    rather than fighting them: 0 ("Creative") is prone to hallucinating
    extra words, 1.0 ("Robust") is documented as less responsive to
    directional prompts. Writes mp3 bytes to out_path. Raises on failure.
    """
    parts = []
    for s in run_segments:
        tag = EMOTION_AUDIO_TAGS.get(s['emotion'], '')
        parts.append(f"{tag} {s['text']}".strip() if tag else s['text'])
    tagged_text = ' '.join(parts)

    req = urllib.request.Request(
        f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
        data=json.dumps({
            'text': tagged_text,
            'model_id': 'eleven_v3',
            'voice_settings': {
                'stability': 0.5,
                'similarity_boost': 0.75,
                'use_speaker_boost': True,
            },
        }).encode(),
        method='POST',
        headers={**_elevenlabs_headers(), 'Content-Type': 'application/json'},
    )
    audio = _elevenlabs_call(req, timeout=90)
    with open(out_path, 'wb') as f:
        f.write(audio)


def elevenlabs_extract_vocals(wav_path, vocals_out_path):
    """Split wav_path into "vocals" and "instrumental" stems via
    ElevenLabs' Stem Separation, keeping only the vocals one — a cleaner
    source than the raw (music-underneath) audio to cut voice-cloning
    samples from, since background music in a cloning sample gets baked
    into the clone's timbre. Written to vocals_out_path as mp3, same
    timeline/duration as wav_path, so segment timestamps computed
    against wav_path (e.g. from Gemini) stay valid against it too.

    stem_variation_id='two_stems_v1' is what gives this vocals/
    instrumental split; the other allowed value, 'six_stems_v1', splits
    into vocals/drums/bass/guitar/piano/other, which would need summing
    5 stems back together for no benefit here — verified against the
    live API, which also confirmed the response is a ZIP containing
    "vocals.mp3" and "instrumental.mp3" (the latter unused here).

    Not required for the pipeline to work — the caller should treat any
    failure here as "clone from the raw audio instead" rather than
    failing the whole job. Raises on failure so the caller can decide.
    """
    boundary = uuid.uuid4().hex
    with open(wav_path, 'rb') as f:
        audio_bytes = f.read()
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="stem_variation_id"\r\n\r\ntwo_stems_v1\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n'
    ).encode() + audio_bytes + f'\r\n--{boundary}--\r\n'.encode()

    req = urllib.request.Request(
        'https://api.elevenlabs.io/v1/music/stem-separation',
        data=body,
        method='POST',
        headers={**_elevenlabs_headers(), 'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    zip_bytes = _elevenlabs_call(req, timeout=180)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf, zf.open('vocals.mp3') as src:
        with open(vocals_out_path, 'wb') as dst:
            dst.write(src.read())


def extract_speaker_clips(wav_path, segments, speaker, out_dir, tag,
                           max_total_duration=120.0, max_clips=10, min_clip_duration=5.0):
    """Cut `speaker`'s lines from wav_path into separate short clip files
    (rather than one pre-concatenated file — see elevenlabs_clone_voice()
    for why), for voice cloning.

    A single natural sentence is often shorter than ElevenLabs' 4.6s
    per-sample minimum ("audio_too_short", verified against the live
    API), especially in a fast back-and-forth conversation — so instead
    of filtering segments individually (which could leave a speaker with
    zero usable clips despite plenty of total speaking time), this first
    groups `segments` (the full, speaker-interleaved, time-ordered list)
    into runs of consecutive entries from the same speaker, then extracts
    each run as ONE continuous cut from its first segment's start to its
    last segment's end. That's safe because a run never crosses a moment
    where a different speaker was talking — unlike concatenating several
    separate extracts together, one continuous cut can't introduce a
    splice artifact. Falls back to the single longest run for a speaker
    if none reach `min_clip_duration` on their own, padding it with a
    bit of surrounding audio if even that longest run is still under
    ElevenLabs' hard 4.6s floor, rather than silently producing no
    clips at all. `speaker` may be None to mean "use the whole track"
    (single-speaker audio — the whole list is one run).
    Returns the list of written file paths (possibly empty)."""
    ELEVENLABS_MIN_SAMPLE_SECONDS = 4.7  # 4.6s + a small safety margin
    runs, current = [], []
    for s in segments:
        if (s.get('speaker') == speaker) if speaker else True:
            current.append(s)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    windows = [(r[0]['start'], r[-1]['end']) for r in runs]
    long_enough = [w for w in windows if w[1] - w[0] >= min_clip_duration]
    if long_enough:
        windows = long_enough
    elif windows:
        # Nothing reaches min_clip_duration — take the single longest run
        # available, and if even that falls short of ElevenLabs' hard
        # 4.6s minimum, pad it with a little surrounding audio (clamped
        # to the file's bounds) rather than sending a clip guaranteed to
        # be rejected as audio_too_short.
        start, end = max(windows, key=lambda w: w[1] - w[0])
        if end - start < ELEVENLABS_MIN_SAMPLE_SECONDS:
            needed = ELEVENLABS_MIN_SAMPLE_SECONDS - (end - start)
            file_info = ffprobe_info(wav_path)
            file_duration = file_info['duration'] if file_info else end
            start = max(0.0, start - needed / 2)
            end = min(file_duration, start + (end - start) + needed)
            start = max(0.0, end - ELEVENLABS_MIN_SAMPLE_SECONDS)
        windows = [(start, end)]
    windows.sort(key=lambda w: w[1] - w[0], reverse=True)

    paths, total = [], 0.0
    for i, (start, end) in enumerate(windows):
        remaining = max_total_duration - total
        if remaining < ELEVENLABS_MIN_SAMPLE_SECONDS or len(paths) >= max_clips:
            break
        dur = min(end - start, remaining)
        clip_path = os.path.join(out_dir, f'vt_td_{tag}_voiceclip_{i}.wav')
        subprocess.run(
            ['ffmpeg', '-y', '-ss', str(start), '-t', str(dur), '-i', wav_path, clip_path],
            capture_output=True
        )
        if os.path.exists(clip_path):
            paths.append(clip_path)
            total += dur
    return paths


def gemini_translate_with_emotion(wav_path, target_language, source_language=None):
    """Transcribe, diarize, translate, and classify emotional delivery in
    one multimodal call — Gemini hears the real audio, so it judges
    delivery (tone/pace/energy) directly instead of guessing from text.
    Returns a list of segment dicts: {start, end, speaker, source_text,
    text, emotion}. Raises on failure."""
    client = _gemini_client()
    uploaded = client.files.upload(file=wav_path)

    source_hint = (
        f' The source audio is primarily in {LANGUAGE_NAMES.get(source_language, source_language)}.'
        if source_language else ''
    )
    target_name = LANGUAGE_NAMES.get(target_language, target_language)
    emotion_list = ', '.join(EMOTION_AUDIO_TAGS.keys())

    prompt = (
        'Listen to this audio. For each natural sentence or phrase, in order, '
        'from start to finish:' + source_hint +
        ' 1) Transcribe it in its original language and script. '
        f'2) Translate it into natural, conversational {target_name}, in '
        f'{target_name}\'s own native script — the way a native speaker '
        'would actually say it, not a stiff literal translation. '
        'MOSTLY (roughly 90-95% of the time, not a strict 100% rule): a '
        'word or phrase that was actually spoken in English in the source '
        'audio should stay in English in the translation too, written in '
        'Latin letters exactly as spoken, rather than being translated '
        f'into a native {target_name} equivalent or transliterated into '
        f'{target_name}\'s script. This matters most for specialized, '
        'technical, or jargon-like terms with no natural everyday '
        f'{target_name} equivalent (e.g. "mutual fund", "trip", "hotel") — '
        'keep those in English. But for simple, common English words that '
        f'have an everyday {target_name} word people would actually use '
        'instead (e.g. "spend" said in the source becoming a native word '
        'like "खर्च" if the target language is Marathi), translating '
        'normally is fine and often sounds more natural — use judgment '
        'the way a fluent bilingual speaker code-switches, not a rigid '
        'rule applied to every single English word. '
        '3) Identify which distinct speaker is talking by voice, labeled '
        'consistently as "Speaker 1", "Speaker 2" etc. in order of first '
        'appearance (omit this field if you can only detect one speaker). '
        '4) Classify the emotional delivery of that line from how it '
        f'actually sounds — tone, pace, energy — as exactly one of: '
        f'{emotion_list}. '
        'Return ONLY a JSON array (no markdown, no commentary) of objects '
        'with keys "start" (seconds, number), "end" (seconds, number), '
        '"speaker" (string, optional), "source_text" (string), "text" '
        f'(string, the {target_name} translation), and "emotion" (string, '
        'one of the list above).'
    )
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=[uploaded, prompt],
    )
    parsed = _parse_gemini_json(response)
    return [
        {
            'start': float(s['start']),
            'end': float(s['end']),
            'speaker': (s.get('speaker') or '').strip() or None,
            'source_text': (s.get('source_text') or '').strip(),
            'text': (s.get('text') or '').strip(),
            'emotion': (s.get('emotion') or 'neutral').strip().lower(),
        }
        for s in parsed
    ]


def segments_to_srt(segments):
    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t % 1) * 1000))
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text'].strip()}\n")
    return '\n'.join(lines)


@app.route('/transcribe', methods=['POST'])
def transcribe_route():
    file = request.files.get('file')
    if not file:
        return jsonify(error='No file uploaded'), 400

    language = request.form.get('language') or None
    romanize = request.form.get('romanize') == '1'
    model    = request.form.get('model') or 'whisper'
    keywords = [k.strip() for k in (request.form.get('keywords') or '').split(',') if k.strip()]

    # Odia isn't in local Whisper's supported language set (checked against
    # whisper.tokenizer.LANGUAGES) — fail fast with a clear message rather
    # than letting Whisper's own exception surface as a generic task error.
    if model == 'whisper' and language == 'or':
        return jsonify(error='Odia isn\'t supported by local Whisper. Select the Gemini model instead.'), 400

    input_path, uid = save_upload(file, fallback_ext='.mp4')
    original_stem = stem(file.filename)
    _tasks[uid] = {'status': 'processing', 'progress': 'Extracting audio…'}

    def run():
        wav_path = os.path.join(TEMP_DIR, f'vt_tr_{uid}.wav')
        t_start = time.time()
        file_size_mb = round(os.path.getsize(input_path) / (1024 * 1024), 2)
        media_info = ffprobe_info(input_path)
        duration_sec = round(media_info['duration'], 1) if media_info else None
        try:
            r = subprocess.run(
                ['ffmpeg', '-y', '-i', input_path,
                 '-ar', '16000', '-ac', '1', '-f', 'wav', wav_path],
                capture_output=True)
            if r.returncode != 0:
                _tasks[uid] = {'status': 'error', 'error': 'Could not extract audio from file.'}
                cleanup_later(input_path)
                return

            if model == 'gemini':
                _tasks[uid]['progress'] = 'Transcribing… (Gemini)'
                segs, detected_language = gemini_transcribe(wav_path, language, romanize)
                # Gemini already wrote the requested script directly — no
                # separate romanization pass needed for this path.
            else:
                _tasks[uid]['progress'] = 'Transcribing… (first run downloads the model)'
                try:
                    import mlx_whisper
                    result = mlx_whisper.transcribe(
                        wav_path,
                        path_or_hf_repo='mlx-community/whisper-large-v3-turbo',
                        language=language,
                        verbose=False,
                    )
                except ImportError:
                    import whisper as _whisper
                    _tasks[uid]['progress'] = 'Transcribing… (loading Whisper model)'
                    try:
                        _model = _whisper.load_model('turbo')
                    except Exception:
                        _model = _whisper.load_model('large-v3')
                    result = _model.transcribe(wav_path, language=language, verbose=False)

                segs = [
                    {'start': s['start'], 'end': s['end'], 'text': s['text'].strip()}
                    for s in result.get('segments', [])
                ]
                detected_language = result.get('language', '')

                if romanize:
                    _tasks[uid]['progress'] = 'Romanizing…'
                    # Whisper always transcribes in native script, so this
                    # always needs a conversion pass. Prefer Gemini for much
                    # more natural output than the local ITRANS library;
                    # fall back to local if no key configured or the call
                    # fails, so this never blocks an otherwise-local job.
                    try:
                        segs = gemini_romanize_segments(segs)
                    except Exception:
                        for s in segs:
                            s['text'] = romanize_text(s['text'])

            if keywords:
                _tasks[uid]['progress'] = 'Keeping keywords in English…'
                try:
                    segs = gemini_enforce_keywords_english(segs, keywords)
                except Exception:
                    # Best-effort — e.g. no Gemini key configured, or the
                    # API call failed. Leave segs as transcribed rather
                    # than fail the whole job over this optional extra.
                    pass

            srt_path = os.path.join(TEMP_DIR, f'vt_tr_{uid}.srt')
            with open(srt_path, 'w', encoding='utf-8') as f:
                f.write(segments_to_srt(segs))

            _tasks[uid] = {
                'status':   'done',
                'result':   srt_path,
                'filename': f'{original_stem}.srt',
                'language': detected_language,
                'segments': segs,
                'stats': {
                    'file_size_mb':          file_size_mb,
                    'duration_sec':          duration_sec,
                    'transcription_time_sec': round(time.time() - t_start, 1),
                },
            }
            cleanup_later(wav_path)
            cleanup_later(input_path)

        except Exception as e:
            _tasks[uid] = {'status': 'error', 'error': str(e)[:300]}
            for p in [input_path, wav_path]:
                try: cleanup_later(p)
                except: pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify(task_id=uid)


@app.route('/transcribe/status/<task_id>')
def transcribe_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify(error='Task not found'), 404
    return jsonify(task)


@app.route('/transcribe/result/<task_id>')
def transcribe_result(task_id):
    task = _tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify(error='Result not ready'), 404
    path     = task['result']
    filename = task.get('filename', 'transcript.srt')
    cleanup_later(path, delay=300)
    _tasks.pop(task_id, None)
    return send_file(path, as_attachment=True, download_name=filename,
                     mimetype='text/plain')


def _stitch_audio_segments(clip_paths, gap_durations, out_path):
    """Concatenate clip_paths in order, inserting a silence gap (seconds,
    capped at 3s) from gap_durations[i] after clip i. Approximates the
    source video's pacing — not exact duration-matching, which belongs to
    the later lip-sync step, not this one."""
    filelist_path = out_path + '.filelist.txt'
    silence_paths = []
    with open(filelist_path, 'w') as f:
        for i, clip_path in enumerate(clip_paths):
            f.write(f"file '{clip_path}'\n")
            gap = min(gap_durations[i], 3.0) if i < len(gap_durations) else 0
            if gap > 0.05:
                silence_path = f'{out_path}.silence_{i}.mp3'
                subprocess.run(
                    ['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                     '-t', str(gap), '-q:a', '9', silence_path],
                    capture_output=True
                )
                f.write(f"file '{silence_path}'\n")
                silence_paths.append(silence_path)
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', filelist_path,
         '-c:a', 'libmp3lame', out_path],
        capture_output=True
    )
    os.remove(filelist_path)
    for p in silence_paths:
        try: os.remove(p)
        except Exception: pass


def _group_segments_into_runs(kept, max_chars=4000):
    """Group consecutive same-speaker segments into runs, each destined
    to become a single TTS call — see elevenlabs_tts() for why merging
    helps flow continuity. A speaker change always starts a new run,
    since it needs a different cloned voice — and so does crossing
    `max_chars`, kept safely under eleven_v3's hard 5,000-character-per-
    request limit (verified against ElevenLabs' docs). Without this cap,
    a long uninterrupted speaker turn on a real full-length video can
    exceed the limit and get silently cut off rather than erroring — the
    dub just stops partway through with no error, which real testing
    surfaced. Returns a list of segment lists."""
    runs, run_lens = [], []
    for seg in kept:
        tag = EMOTION_AUDIO_TAGS.get(seg.get('emotion'), '')
        seg_len = len(seg['text']) + (len(tag) + 1 if tag else 0)
        same_speaker = runs and runs[-1][-1].get('speaker') == seg.get('speaker')
        fits = same_speaker and run_lens[-1] + 1 + seg_len <= max_chars
        if fits:
            runs[-1].append(seg)
            run_lens[-1] += 1 + seg_len
        else:
            runs.append([seg])
            run_lens.append(seg_len)
    return runs


@app.route('/translate-dub', methods=['POST'])
def translate_dub_route():
    file = request.files.get('file')
    if not file:
        return jsonify(error='No file uploaded'), 400

    target_language = request.form.get('target_language')
    if not target_language:
        return jsonify(error='Target language is required'), 400
    source_language = request.form.get('source_language') or None

    input_path, uid = save_upload(file, fallback_ext='.mp4')
    _tasks[uid] = {'status': 'processing', 'progress': 'Extracting audio…'}

    def run():
        wav_path = os.path.join(TEMP_DIR, f'vt_td_{uid}.wav')
        try:
            r = subprocess.run(
                ['ffmpeg', '-y', '-i', input_path,
                 '-ar', '44100', '-ac', '1', '-f', 'wav', wav_path],
                capture_output=True)
            if r.returncode != 0:
                _tasks[uid] = {'status': 'error', 'error': 'Could not extract audio from file.'}
                cleanup_later(input_path)
                return

            _tasks[uid]['progress'] = 'Transcribing, translating, and reading emotion… (Gemini)'
            segments = gemini_translate_with_emotion(wav_path, target_language, source_language)

            # Separate vocals from background music up front, before
            # cloning — the clean vocals stem is a better source to cut
            # cloning samples from than the raw audio with music playing
            # underneath the speaker. Best-effort: if this fails, clone
            # straight from the raw audio like before (voice-add's own
            # remove_background_noise=true is still applied as a fallback
            # layer of cleaning either way) — never fail the whole job
            # over this.
            _tasks[uid]['progress'] = 'Separating vocals from background music… (ElevenLabs)'
            vocals_path = os.path.join(TEMP_DIR, f'vt_td_{uid}_vocals.mp3')
            try:
                elevenlabs_extract_vocals(wav_path, vocals_path)
                clone_source = vocals_path
            except Exception:
                vocals_path = None
                clone_source = wav_path

            _tasks[uid]['progress'] = 'Cloning speaker voice(s)… (ElevenLabs)'
            speakers = sorted({s['speaker'] for s in segments if s['speaker']})
            voice_ids = {}
            for spk in (speakers or [None]):
                clip_paths = extract_speaker_clips(clone_source, segments, spk, TEMP_DIR, f'{uid}_{spk or "solo"}')
                if clip_paths:
                    voice_ids[spk] = elevenlabs_clone_voice(clip_paths, f'{uid}-{spk or "speaker"}')
                for p in clip_paths:
                    cleanup_later(p, delay=5)
            if vocals_path:
                cleanup_later(vocals_path, delay=5)

            if not voice_ids:
                _tasks[uid] = {'status': 'error', 'error': 'Could not extract enough clean speaker audio to clone a voice.'}
                cleanup_later(wav_path)
                cleanup_later(input_path)
                return

            # A freshly cloned voice can take ~10-15s to propagate through
            # ElevenLabs' backend before synthesis is reliable — per their
            # own guidance. Without this wait, the very first TTS call for
            # a new voice (typically the video's opening line) can come
            # out flatter/less expressive than every line after it, which
            # is exactly what real testing on this feature surfaced.
            _tasks[uid]['progress'] = 'Finishing up voice setup…'
            time.sleep(15)

            _tasks[uid] = {
                'status': 'transcript_ready',
                'segments': segments,
                'target_language': target_language,
                '_voice_ids': voice_ids,
            }
            cleanup_later(wav_path)
            cleanup_later(input_path)
        except Exception as e:
            _tasks[uid] = {'status': 'error', 'error': str(e)[:300]}
            for p in [input_path, wav_path]:
                try: cleanup_later(p)
                except: pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify(task_id=uid)


@app.route('/translate-dub/status/<task_id>')
def translate_dub_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify(error='Task not found'), 404
    # Don't leak internal bookkeeping (ElevenLabs voice IDs) to the client.
    return jsonify({k: v for k, v in task.items() if not k.startswith('_')})


@app.route('/translate-dub/generate-audio/<task_id>', methods=['POST'])
def translate_dub_generate_audio(task_id):
    task = _tasks.get(task_id)
    if not task or task.get('status') != 'transcript_ready':
        return jsonify(error='Transcript not ready for this task'), 404

    edited_segments = (request.get_json(silent=True) or {}).get('segments')
    if not edited_segments:
        return jsonify(error='No segments provided'), 400

    voice_ids = task.get('_voice_ids', {})
    task['status']   = 'processing_audio'
    task['progress'] = 'Generating speech… (ElevenLabs)'

    def run():
        clip_paths = []
        try:
            kept = [s for s in edited_segments if (s.get('text') or '').strip()]
            runs = _group_segments_into_runs(kept)

            for i, run_segs in enumerate(runs):
                task['progress'] = f'Generating speech… ({i + 1}/{len(runs)})'
                voice_id = (voice_ids.get(run_segs[0].get('speaker')) or voice_ids.get(None)
                            or next(iter(voice_ids.values())))
                clip_path = os.path.join(TEMP_DIR, f'vt_td_{task_id}_clip_{i}.mp3')
                elevenlabs_tts(run_segs, voice_id, clip_path)
                clip_paths.append(clip_path)

            gaps = [
                max(0.0, runs[i + 1][0]['start'] - runs[i][-1]['end']) if i + 1 < len(runs) else 0.0
                for i in range(len(runs))
            ]

            out_path = os.path.join(TEMP_DIR, f'vt_td_{task_id}.mp3')
            _stitch_audio_segments(clip_paths, gaps, out_path)

            _tasks[task_id] = {
                'status':   'done',
                'result':   out_path,
                'filename': 'translated_audio.mp3',
                'segments': kept,
            }
            for p in clip_paths:
                cleanup_later(p, delay=5)
        except Exception as e:
            _tasks[task_id] = {'status': 'error', 'error': str(e)[:300]}
            for p in clip_paths:
                try: cleanup_later(p, delay=5)
                except: pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify(status='processing_audio')


@app.route('/translate-dub/result/<task_id>')
def translate_dub_result(task_id):
    task = _tasks.get(task_id)
    if not task or task.get('status') != 'done':
        return jsonify(error='Result not ready'), 404
    # Not popped on read (unlike /transcribe/result) — the UI fetches this
    # same URL both for an inline <audio> preview and for the download
    # link, so it needs to stay servable more than once. Cleaned up by the
    # scheduled cleanup_later() call instead.
    return send_file(task['result'], as_attachment=True,
                     download_name=task.get('filename', 'translated_audio.mp3'),
                     mimetype='audio/mpeg')


# ── Titles & Description ────────────────────────────────────────────────────
#
# A separate, standalone feature from Transcribe and Translate & Dub — takes
# a plain script (no video/audio involved) and generates YouTube title,
# thumbnail text, and description options via a single Gemini text call.
# Fast enough (a few seconds) to run synchronously rather than through the
# async task/polling pattern the video-processing features use.

def gemini_generate_metadata(script_text):
    """Generate 3 options each for video title, thumbnail text, and
    description from a script, via Gemini. Returns a dict with keys
    "titles", "thumbnail_texts", "descriptions", each a list of up to 3
    strings. Raises on failure."""
    client = _gemini_client()
    prompt = (
        'Here is a video script. Based on it, generate YouTube content:\n\n'
        '1) "titles": 3 distinct video title options — attention-grabbing '
        'but accurate to the content, each under 70 characters.\n'
        '2) "thumbnail_texts": 3 distinct short thumbnail overlay text '
        'options — punchy, 2-6 words, the kind of bold text layered on a '
        'YouTube thumbnail image, not a full sentence.\n'
        '3) "descriptions": 3 distinct video description options for the '
        'YouTube description box — 2-4 sentences each, summarizing the '
        'video and hooking the viewer to watch.\n\n'
        'Return ONLY a JSON object (no markdown, no commentary) with keys '
        '"titles", "thumbnail_texts", "descriptions", each an array of '
        'exactly 3 strings.\n\nSCRIPT:\n' + script_text
    )
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=[prompt],
    )
    parsed = _parse_gemini_json(response)
    return {
        key: [str(t).strip() for t in parsed.get(key, [])][:3]
        for key in ('titles', 'thumbnail_texts', 'descriptions')
    }


@app.route('/generate-metadata', methods=['POST'])
def generate_metadata_route():
    script_text = (request.form.get('script') or '').strip()
    if not script_text:
        return jsonify(error='Please paste your script text first.'), 400
    try:
        return jsonify(gemini_generate_metadata(script_text))
    except Exception as e:
        return jsonify(error=str(e)[:300]), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('\n🎬  EditOps — Money Mediia')
    print('─' * 32)

    # Check for updates from GitHub
    auto_update()
    start_periodic_auto_update()

    # Quick ffmpeg check
    check = subprocess.run(['ffmpeg', '-version'], capture_output=True)
    if check.returncode != 0:
        print('\n⚠️  ffmpeg not found! Please install it first.')
        print('   Mac:     brew install ffmpeg')
        print('   Windows: https://ffmpeg.org/download.html\n')

    # On Windows only, prefer Waitress (a production WSGI server) over
    # Flask's built-in dev server — Werkzeug's own dev server explicitly
    # warns it isn't built for this, and on a shared Windows machine acting
    # as a real server for multiple teammates, its I/O handling is a
    # measurable bottleneck for large video uploads. macOS/Linux are
    # untouched — every teammate running their own local copy keeps today's
    # exact dev-server behavior; this only takes a different path when
    # os.name == 'nt'.
    if os.name == 'nt':
        try:
            from waitress import serve
            print('\n✅  Starting server (Waitress — production WSGI server)...')
            print('👉  Open your browser: http://localhost:5001')
            print('    (Press Ctrl+C to stop)\n')
            serve(app, host='0.0.0.0', port=5001, threads=6)
        except ImportError:
            print('\n⚠️  Waitress not installed — falling back to the dev server.')
            print('   For faster uploads, run: pip install waitress\n')
            print('✅  Starting server...')
            print('👉  Open your browser: http://localhost:5001')
            print('    (Press Ctrl+C to stop)\n')
            app.run(debug=False, host='0.0.0.0', port=5001, threaded=True)
    else:
        print('\n✅  Starting server...')
        print('👉  Open your browser: http://localhost:5001')
        print('    (Press Ctrl+C to stop)\n')
        app.run(debug=False, host='0.0.0.0', port=5001, threaded=True)
