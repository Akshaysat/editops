from flask import Flask, request, render_template, send_file, jsonify
import subprocess, os, uuid, json, tempfile, threading, time, sys, glob, shutil
import urllib.request, urllib.error, urllib.parse

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
    """Call the Supabase REST (PostgREST) API for the `feedback` table."""
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

        print(f'⬇️   {commits_behind} update(s) found. Pulling latest version...')
        subprocess.run(['git', 'pull', 'origin', 'main'],
                       cwd=repo_dir, capture_output=True, timeout=30)

        # Re-install dependencies in case requirements.txt changed
        # Only re-install if a requirements file changed in this pull
        req_changed = subprocess.run(
            ['git', 'diff', 'HEAD~1', 'HEAD', '--name-only'],
            cwd=repo_dir, capture_output=True, text=True).stdout
        req_file = 'requirements-windows.txt' if os.name == 'nt' else 'requirements.txt'
        pip_bin  = 'Scripts\\pip' if os.name == 'nt' else os.path.join('venv', 'bin', 'pip')
        pip      = os.path.join(repo_dir, pip_bin)
        if 'requirements' in req_changed and os.path.exists(pip):
            print('📦  Updating dependencies...')
            subprocess.run([pip, 'install', '-r',
                            os.path.join(repo_dir, req_file), '-q'],
                           cwd=repo_dir, timeout=600)

        print('🔁  Restarting app with latest version...\n')
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        print(f'⚠️   Update check failed (continuing anyway): {e}')


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

    # Use Apple VideoToolbox hardware encoder on macOS (5-10x faster than libx264)
    vcodec = ['-c:v', 'h264_videotoolbox', '-b:v', bv, '-allow_sw', '1'] \
             if sys.platform == 'darwin' else \
             ['-c:v', 'libx264', '-b:v', bv, '-preset', 'fast']

    cmd = ['ffmpeg', '-y', *t_limit, '-i', input_path,
           '-filter_complex', fc, *maps,
           *vcodec,
           '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
           output_path]

    r = subprocess.run(cmd, capture_output=True)
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

    start = request.form.get('start', '').strip()
    end   = request.form.get('end', '').strip()

    if not start and not end:
        os.remove(input_path)
        return jsonify(error='Please provide a start time, end time, or both.'), 400

    info = ffprobe_info(input_path)
    if info and not info['has_video']:
        out_ext = os.path.splitext(file.filename)[1] or '.mp3'
    else:
        out_ext = '.mp4'

    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}{out_ext}')

    cmd = ['ffmpeg', '-y']
    if start:
        cmd += ['-ss', start]
    cmd += ['-i', input_path]
    if end:
        cmd += ['-to', end]
    cmd += ['-c', 'copy', output_path]

    r = subprocess.run(cmd, capture_output=True)
    cleanup_later(input_path)

    if r.returncode != 0:
        return jsonify(error='Trim failed.'), 500

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True,
                     download_name=f'{stem(file.filename)}_trimmed{out_ext}')


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
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_path,
               '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
               '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
               output_path]
        download_name = 'merged_video.mp4'
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
    vcodec = ['-c:v', 'h264_videotoolbox', '-b:v', bv, '-allow_sw', '1'] \
             if sys.platform == 'darwin' else \
             ['-c:v', 'libx264', '-b:v', bv, '-preset', 'fast']

    cmd = ['ffmpeg', '-y',
           '-i', video_path,
           '-loop', '1', '-t', str(duration), '-i', image_path,
           '-filter_complex', fc, *maps,
           *vcodec,
           '-c:a', 'aac', '-b:a', '192k',
           '-movflags', '+faststart',
           output_path]

    r = subprocess.run(cmd, capture_output=True)
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
        cmd = ['ffmpeg', '-y', '-i', input_path, '-vn', '-c:a', cfg['acodec']]
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
        cmd = ['ffmpeg', '-y', '-i', input_path,
               '-c:v', cfg['vcodec'], '-preset', 'fast', '-crf', '18',
               '-c:a', cfg['acodec'], '-b:a', '192k',
               '-movflags', '+faststart', output_path]
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


def qa_check_spelling(segments, max_unknown_ratio=0.5, min_words_for_ratio=4):
    """Like check_spelling, but skips a whole line when most of its words
    aren't recognized by the English dictionary — much more likely a
    non-English (e.g. Hinglish) sentence than one riddled with typos, so
    flagging individual words in it would mostly be noise. HINGLISH_WORDS
    remains a second layer for isolated Hinglish words mixed into an
    otherwise-English line, which the ratio check alone wouldn't catch.

    The ratio only applies once there are enough checkable words to make it
    a meaningful signal — a short title with two typos ("Wellcome to the
    shwo") can easily hit a >50% unknown ratio on its own merits without
    being remotely non-English, so lines below min_words_for_ratio always
    fall back to flagging each unknown word individually."""
    try:
        from spellchecker import SpellChecker
        import re
        spell = SpellChecker()
        issues = []
        for i, seg in enumerate(segments):
            words = re.findall(r"[A-Za-z']+", seg['text'])
            to_check = [w.lower().strip("'") for w in words
                        if len(w) > 2 and not w.isupper()]
            if not to_check:
                continue
            unknown = spell.unknown(to_check)
            if len(to_check) >= min_words_for_ratio and len(unknown) / len(to_check) > max_unknown_ratio:
                continue
            for word in unknown:
                if word in HINGLISH_WORDS:
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


def qa_detect_subtitle_band(raw_results, total_frames, min_frac=0.35):
    """Auto-detects this video's actual subtitle Y-position instead of
    assuming a fixed percentage: subtitles are rendered by the same
    template in the same spot every time they appear, so they cluster
    tightly in the bottom half of frame across many sampled frames — a
    one-off graphic won't recur at the same position anywhere near as
    often. Returns the dominant bucket, or None if nothing clearly
    dominates (in which case nothing gets excluded — safer to risk a
    missed subtitle than to blindly exclude real graphic text)."""
    buckets = {}
    for _ts, _frame_path, bbox, _text, frame_h in raw_results:
        y_mid = sum(p[1] for p in bbox) / len(bbox)
        if y_mid < frame_h * 0.6:
            continue
        b = qa_y_bucket(y_mid, frame_h)
        buckets[b] = buckets.get(b, 0) + 1

    if not buckets:
        return None
    bucket, count = max(buckets.items(), key=lambda kv: kv[1])
    if count / max(1, total_frames) < min_frac:
        return None
    return bucket


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


# OCR confidence below this is more likely a mid-animation blur/partial-
# render artifact than genuine text — settled, cleanly-rendered text reads
# with much higher confidence in practice (0.8+ typically). Also happens to
# catch small dense text like disclaimers: confidence drops with size even
# when the text is fully legible, since OCR is just less certain about it.
QA_MIN_OCR_CONFIDENCE = 0.6

# Text shorter than this fraction of frame height is treated as fine print
# (legal disclaimers, copyright notices) rather than a graphic — kept as a
# second, independent check in case dense small text somehow still scores
# high confidence (e.g. very crisp text in a high-res source).
QA_MIN_TEXT_HEIGHT_FRAC = 0.03


def qa_scan_frames(frames, progress_cb=None):
    """Runs OCR on each sampled frame, auto-detects and excludes this
    video's subtitle band, spellchecks what's left (skipping likely-
    non-English lines — see qa_check_spelling), and returns deduped
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
            if conf < QA_MIN_OCR_CONFIDENCE:
                continue
            ys = [p[1] for p in bbox]
            if (max(ys) - min(ys)) < frame_h * QA_MIN_TEXT_HEIGHT_FRAC:
                continue
            raw_results.append((ts, frame_path, bbox, text, frame_h))

    subtitle_bucket = qa_detect_subtitle_band(raw_results, len(frames))

    ocr_segments = []   # [{'text', 'start'}] — qa_check_spelling's expected shape
    frame_meta   = []   # parallel to ocr_segments: (frame_path, bbox)
    for ts, frame_path, bbox, text, frame_h in raw_results:
        if subtitle_bucket is not None:
            y_mid = sum(p[1] for p in bbox) / len(bbox)
            if qa_y_bucket(y_mid, frame_h) == subtitle_bucket:
                continue
        ocr_segments.append({'text': text, 'start': ts})
        frame_meta.append((frame_path, bbox))

    raw_issues = qa_check_spelling(ocr_segments)

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


# ── Transcription ────────────────────────────────────────────────────────────

def romanize_text(text):
    """Convert Devanagari characters to Roman (ITRANS). Latin chars pass through unchanged."""
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        return transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        return text


def check_spelling(segments):
    """Return list of {seg_idx, word, suggestions, start} for misspelled English words."""
    try:
        from spellchecker import SpellChecker
        import re
        spell = SpellChecker()
        issues = []
        for i, seg in enumerate(segments):
            # Only check Latin-script words — Devanagari/Hindi passes through untouched
            words = re.findall(r"[A-Za-z']+", seg['text'])
            to_check = [w.lower().strip("'") for w in words
                        if len(w) > 2 and not w.isupper()]
            for word in spell.unknown(to_check):
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
    input_path, uid = save_upload(file, fallback_ext='.mp4')
    original_stem = stem(file.filename)
    _tasks[uid] = {'status': 'processing', 'progress': 'Extracting audio…'}

    def run():
        wav_path = os.path.join(TEMP_DIR, f'vt_tr_{uid}.wav')
        try:
            r = subprocess.run(
                ['ffmpeg', '-y', '-i', input_path,
                 '-ar', '16000', '-ac', '1', '-f', 'wav', wav_path],
                capture_output=True)
            if r.returncode != 0:
                _tasks[uid] = {'status': 'error', 'error': 'Could not extract audio from file.'}
                cleanup_later(input_path)
                return

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
            if romanize:
                _tasks[uid]['progress'] = 'Romanizing…'
                for s in segs:
                    s['text'] = romanize_text(s['text'])

            _tasks[uid]['progress'] = 'Checking spelling…'
            spell_issues = check_spelling(segs)

            srt_path = os.path.join(TEMP_DIR, f'vt_tr_{uid}.srt')
            with open(srt_path, 'w', encoding='utf-8') as f:
                f.write(segments_to_srt(segs))

            _tasks[uid] = {
                'status':          'done',
                'result':          srt_path,
                'filename':        f'{original_stem}.srt',
                'language':        result.get('language', ''),
                'segments':        segs,
                'spelling_issues': spell_issues,
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('\n🎬  EditOps — Money Mediia')
    print('─' * 32)

    # Check for updates from GitHub
    auto_update()

    # Quick ffmpeg check
    check = subprocess.run(['ffmpeg', '-version'], capture_output=True)
    if check.returncode != 0:
        print('\n⚠️  ffmpeg not found! Please install it first.')
        print('   Mac:     brew install ffmpeg')
        print('   Windows: https://ffmpeg.org/download.html\n')
    else:
        print('\n✅  Starting server...')
        print('👉  Open your browser: http://localhost:5001')
        print('    (Press Ctrl+C to stop)\n')

    app.run(debug=False, host='0.0.0.0', port=5001, threaded=True)
