from flask import Flask, request, render_template, send_file, jsonify
import subprocess, os, uuid, json, tempfile, threading, time, sys, glob

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 * 1024  # 20 GB max upload

TEMP_DIR = tempfile.gettempdir()
NULL_DEV = 'NUL' if os.name == 'nt' else '/dev/null'


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

        print('🔁  Restarting app with latest version...\n')
        time.sleep(1)
        # Restart the current process with the same arguments
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
    """Return dict with duration, bit_rate, has_audio for a video file."""
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
    return {
        'duration': float(fmt.get('duration') or 0),
        'bit_rate':  int(fmt.get('bit_rate')  or 0),
        'has_audio': audio is not None,
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
        return jsonify(error='No video uploaded'), 400

    input_path, uid = save_upload(file)
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')

    info = ffprobe_info(input_path)
    if not info:
        os.remove(input_path)
        return jsonify(error='Cannot read video file. Is it a valid video?'), 400

    mode = request.form.get('mode', 'multiplier')
    raw  = request.form.get('value', '')

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

    cmd = ['ffmpeg', '-y', '-i', input_path,
           '-filter_complex', fc, *maps,
           '-c:v', 'libx264', '-b:v', bv, '-preset', 'fast',
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
        return jsonify(error='No video uploaded'), 400

    input_path, uid = save_upload(file)
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')
    passlog     = os.path.join(TEMP_DIR, f'vt_pass_{uid}')

    info = ffprobe_info(input_path)
    if not info or info['duration'] == 0:
        os.remove(input_path)
        return jsonify(error='Cannot read video file.'), 400

    target_mb  = float(request.form.get('target_mb', 900))
    total_bits = target_mb * 1_000_000 * 8          # decimal MB
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
        return jsonify(error='No video uploaded'), 400

    input_path, uid = save_upload(file)
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}.mp4')

    start = request.form.get('start', '').strip()
    end   = request.form.get('end', '').strip()

    if not start and not end:
        os.remove(input_path)
        return jsonify(error='Please provide a start time, end time, or both.'), 400

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
                     download_name=f'{stem(file.filename)}_trimmed.mp4')


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


@app.route('/convert', methods=['POST'])
def convert_route():
    file = request.files.get('video')
    if not file:
        return jsonify(error='No video uploaded'), 400

    target_fmt = request.form.get('format', 'mp4').lower().strip('.')
    SUPPORTED = {
        'mp4':  {'vcodec': 'libx264', 'acodec': 'aac',          'ext': '.mp4'},
        'mov':  {'vcodec': 'libx264', 'acodec': 'aac',          'ext': '.mov'},
        'avi':  {'vcodec': 'libxvid', 'acodec': 'mp3',          'ext': '.avi'},
        'mkv':  {'vcodec': 'libx264', 'acodec': 'aac',          'ext': '.mkv'},
        'webm': {'vcodec': 'libvpx-vp9', 'acodec': 'libopus',  'ext': '.webm'},
        'gif':  {'vcodec': None,      'acodec': None,            'ext': '.gif'},
    }

    if target_fmt not in SUPPORTED:
        return jsonify(error=f'Unsupported format. Choose from: {", ".join(SUPPORTED)}'), 400

    input_path, uid = save_upload(file)
    cfg = SUPPORTED[target_fmt]
    output_path = os.path.join(TEMP_DIR, f'vt_out_{uid}{cfg["ext"]}')

    if target_fmt == 'gif':
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


@app.route('/ytdl', methods=['POST'])
def ytdl_route():
    url = request.form.get('url', '').strip()
    quality = request.form.get('quality', '720')

    if not url:
        return jsonify(error='Please provide a YouTube URL.'), 400

    uid = str(uuid.uuid4())
    out_tmpl = os.path.join(TEMP_DIR, f'vt_yt_{uid}.%(ext)s')

    if quality == 'audio':
        cmd = ['yt-dlp', '-x', '--audio-format', 'mp3', '--audio-quality', '0',
               '-o', out_tmpl, url]
    else:
        cmd = ['yt-dlp',
               '-f', f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]',
               '--merge-output-format', 'mp4',
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

    # Get a clean filename from the video title
    title_r = subprocess.run(['yt-dlp', '--get-title', url],
                             capture_output=True, text=True, timeout=30)
    raw_title = title_r.stdout.strip() or 'video'
    safe_title = ''.join(c for c in raw_title if c.isalnum() or c in ' -_').strip()[:80]
    download_name = f'{safe_title}{ext}' if safe_title else f'video{ext}'

    cleanup_later(output_path)
    return send_file(output_path, as_attachment=True, download_name=download_name)


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
