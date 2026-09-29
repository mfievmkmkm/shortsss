#!/usr/bin/env python3
"""Telegram shorts generator: storyboard -> art -> speech -> FFmpeg -> preview."""
import base64
import json
import os
import re
import queue
import threading
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
AI_KEY = os.getenv('POLZA_API_KEY', '')
ALLOWED = {x.strip() for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()}
TEXT_MODEL = os.getenv('TEXT_MODEL', 'openai/gpt-4o-mini')
IMAGE_MODEL = os.getenv('IMAGE_MODEL', 'dall-e-3')
VOICE_MODEL = os.getenv('VOICE_MODEL', 'openai/gpt-4o-mini-tts')
VOICE = os.getenv('VOICE', 'alloy')
STATE = Path(os.getenv('STATE_DIR', './state'))
STATE.mkdir(exist_ok=True, parents=True)

JOBS = queue.Queue(maxsize=4)

STYLES = {
    'story': 'Кинематографичная микроистория: интрига сразу, конкретные события, эмоциональный поворот и финал. Иллюстрированные кадры. 6 сцен.',
    'fact': 'Один удивительный проверяемый факт, объяснение без преувеличений и короткий вывод. 5 сцен.',
    'list': 'Практичная подборка из 3 пунктов, каждый с неожиданной конкретной деталью. 6 сцен.',
}

def request(url, payload=None, headers=None, timeout=180):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            return json.loads(raw) if 'json' in res.headers.get('Content-Type', '') else raw
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read(500).decode(errors="replace")}') from e

def ai(path, payload, binary=False):
    result = request('https://polza.ai/api/' + ('v2/' if path.startswith('images/') else 'v1/') + path, payload,
                     {'Authorization': 'Bearer ' + AI_KEY, 'Content-Type': 'application/json'}, timeout=240)
    return result

def tg(method, payload):
    result = request('https://api.telegram.org/bot' + BOT_TOKEN + '/' + method, payload,
                     {'Content-Type': 'application/json'}, timeout=130)
    if not result.get('ok'):
        raise RuntimeError(result.get('description', 'Telegram error'))
    return result['result']

def send(chat, message, keyboard=None):
    p = {'chat_id': chat, 'text': message[:4000]}
    if keyboard:
        p['reply_markup'] = {'inline_keyboard': keyboard}
    return tg('sendMessage', p)

def upload_video(chat, filename, caption):
    boundary = 'shortsbotboundary' + str(int(time.time()))
    body = bytearray()
    for key, value in [('chat_id', str(chat)), ('caption', caption[:1024]), ('supports_streaming', 'true')]:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="short.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
    body += Path(filename).read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request('https://api.telegram.org/bot' + BOT_TOKEN + '/sendVideo', data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urllib.request.urlopen(req, timeout=300) as res:
        result = json.load(res)
    if not result.get('ok'):
        raise RuntimeError(str(result))

def chat_json(prompt):
    r = ai('chat/completions', {'model':TEXT_MODEL,
        'messages':[{'role':'system','content':'Ты редактор коротких видео. Пиши конкретно, без штампов и обмана.'},
                    {'role':'user','content':prompt}],
        'response_format':{'type':'json_object'}})
    return json.loads(r['choices'][0]['message']['content'])

def screenplay(topic, style):
    prompt = f'''Создай сценарий вертикального видео 25–45 секунд. Тема: {topic}. Режим: {STYLES[style]}.
Сначала придумай три РАЗНЫХ хука: открытая петля, резкое наблюдение, конкретная сцена. Выбери лучший, который понятен без контекста за 3 секунды. Не начинай со слов «а вы знали», «представьте», «сегодня поговорим». Не обещай того, чего ролик не раскроет.
Верни только JSON: {{"title":"...","caption":"...","visual_bible":"постоянный стиль, эпоха, палитра, герой и детали одежды", "hook_options":["...","...","..."],"scenes":[{{"voice":"...","screen":"...","visual":"...","beat":"..."}}]}}.
Сцена 0: хук из 3–7 слов, его voice можно произнести за 3 секунды; screen 1–4 слова; visual сразу показывает конфликт или странный предмет. Начинай действие без вступления. Далее каждые 3–6 секунд новая информация или визуальный поворот. Во всех сценах voice 1–2 короткие разговорные фразы, screen 1–4 слова, visual описывает конкретный кадр, beat задаёт монтажный акцент. Всего {6 if style != 'fact' else 5} сцен, не больше 105 слов озвучки. Финал закрывает вопрос, а не обрывается на пустом тизере.
Русский разговорный язык, без канцелярита, списков при режиме истории, пафоса, повтора одних и тех же вводных фраз. Если тема фактологическая и источников нет, избегай точных цифр, цитат и неподтверждённых подробностей; не выдавай вымысел за факт. Реклама только по явному запросу.'''
    obj = chat_json(prompt)
    if not isinstance(obj.get('scenes'), list) or not 4 <= len(obj['scenes']) <= 8:
        raise ValueError('Некорректное число сцен')
    for s in obj['scenes']:
        if not all(isinstance(s.get(k), str) and s[k].strip() for k in ('voice','screen','visual')):
            raise ValueError('Некорректная сцена')
    if len(obj['scenes'][0]['voice'].split()) > 7 or len(obj['scenes'][0]['screen'].split()) > 4:
        raise ValueError('Хук слишком длинный для первых трёх секунд')
    if sum(len(s['voice'].split()) for s in obj['scenes']) > 115:
        raise ValueError('Сценарий слишком длинный')
    return obj

def make_image(scene, style, path, visual_bible=''):
    art = {'story':'rich atmospheric editorial illustration, cinematic lighting, painterly realism',
           'fact':'clean editorial photography, tactile objects, realistic lighting',
           'list':'vibrant editorial collage, clear visual focus, realistic textures'}[style]
    prompt = f'Portrait 9:16 vertical composition. {art}. {scene["visual"]}. Series style bible: {visual_bible}. Consistent visual style. No words, no letters, no logos, no watermark. Center main subject; leave lower middle area calm for a caption.'
    r = ai('images/generations', {'model': IMAGE_MODEL, 'prompt':prompt, 'size':os.getenv('IMAGE_SIZE', '1024x1792'), 'n':1, 'response_format':'b64_json'})
    data = r['data'][0]
    if data.get('b64_json'):
        path.write_bytes(base64.b64decode(data['b64_json']))
    elif data.get('url'):
        with urllib.request.urlopen(data['url'], timeout=120) as res:
            path.write_bytes(res.read())
    else:
        raise ValueError('Polza не вернула изображение')

def make_voice(line, path):
    r = ai('audio/speech', {'model':VOICE_MODEL, 'voice':VOICE, 'input':line,
                              'instructions':'Говори по-русски живо, быстро и естественно; без дикторского пафоса.',
                              'response_format':'mp3', 'language_code':'ru'})
    if r.get('contentType') != 'audio/mpeg' or not r.get('audio'):
        raise ValueError('Polza не вернула MP3 озвучку')
    path.write_bytes(base64.b64decode(r['audio']))

def duration(path):
    p = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(path)],
                       capture_output=True,text=True,check=True)
    return float(p.stdout.strip())

def caption_filter(words, font):
    # drawtext escaping is awkward; a separate text file avoids injected FFmpeg expressions.
    return f"drawtext=fontfile='{font}':textfile='{words}':reload=0:fontcolor=white:fontsize=70:borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.73-text_h/2"

def render(story, style, folder):
    font = os.getenv('FONT_PATH', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
    if not Path(font).is_file():
        raise RuntimeError('Укажите путь к TTF шрифту в FONT_PATH')
    clips = []
    for index, scene in enumerate(story['scenes']):
        image = folder / f'art_{index}.png'
        speech = folder / f'voice_{index}.mp3'
        words = folder / f'caption_{index}.txt'
        clip = folder / f'clip_{index}.mp4'
        make_image(scene, style, image, story.get('visual_bible',''))
        make_voice(scene['voice'], speech)
        words.write_text(re.sub(r'[\r\n]+',' ',scene['screen'])[:36], encoding='utf-8')
        seconds = duration(speech) + 0.35
        frames = max(1, round(seconds * 30))
        vf = (f'scale=1180:2100:force_original_aspect_ratio=increase,crop=1180:2100,'
              f'zoompan=z=\'min(zoom+0.0007,1.10)\':d={frames}:s=1080x1920:fps=30,'
              f'{caption_filter(words, font)},format=yuv420p')
        subprocess.run(['ffmpeg','-y','-hide_banner','-loglevel','error','-loop','1','-i',str(image),
                        '-i',str(speech),'-filter_complex',f'[0:v]{vf}[v];[1:a]apad=pad_dur=0.35[a]',
                        '-map','[v]','-map','[a]','-t',f'{seconds:.3f}',
                        '-c:v','libx264','-preset','veryfast','-crf','24','-r','30','-c:a','aac','-b:a','128k',
                        '-movflags','+faststart',str(clip)],check=True)
        clips.append(clip)
    concat = folder / 'concat.txt'
    concat.write_text(''.join(f"file '{p.name}'\n" for p in clips))
    output = folder / 'short.mp4'
    subprocess.run(['ffmpeg','-y','-hide_banner','-loglevel','error','-f','concat','-safe','0','-i',str(concat),
                    '-c','copy','-movflags','+faststart',str(output)],check=True)
    return output

def process(chat, style, topic):
    send(chat, 'Пишу сценарий и собираю сцены. Это может занять несколько минут.')
    story = screenplay(topic, style)
    with tempfile.TemporaryDirectory(prefix='shorts-', dir=STATE) as temp:
        path = render(story, style, Path(temp))
        upload_video(chat, path, story['title'] + '\n\n' + story.get('caption',''))
    send(chat, 'Черновик готов. Чтобы сделать другую версию: /new')

def worker_loop():
    while True:
        chat, style, topic = JOBS.get()
        try:
            process(chat, style, topic)
        except Exception as e:
            print('Generation failed:', repr(e), flush=True)
            try:
                send(chat, 'Не получилось собрать ролик. Проверь Polza, баланс и FFmpeg; подробности в логах. Попробуй /new.')
            except Exception:
                pass
        finally:
            JOBS.task_done()

def main():
    if not BOT_TOKEN or not AI_KEY or not ALLOWED:
        raise SystemExit('Нужны TELEGRAM_BOT_TOKEN, POLZA_API_KEY и ALLOWED_USER_IDS')
    threading.Thread(target=worker_loop, daemon=True).start()
    offset_file = STATE / 'offset.txt'
    offset = int(offset_file.read_text()) if offset_file.exists() else 0
    pending = {}
    print('Bot polling started', flush=True)
    while True:
        try:
            updates = tg('getUpdates', {'offset':offset,'timeout':45,'allowed_updates':['message','callback_query']})
            for update in updates:
                offset = update['update_id'] + 1
                offset_file.write_text(str(offset))
                callback = update.get('callback_query')
                message = update.get('message', {})
                user = (callback or message).get('from', {})
                chat = (callback or {}).get('message', {}).get('chat', {}).get('id') or message.get('chat',{}).get('id')
                if callback:
                    tg('answerCallbackQuery', {'callback_query_id':callback['id']})
                if ALLOWED and str(user.get('id')) not in ALLOWED:
                    send(chat,'Доступ к этому боту ограничен.')
                    continue
                if callback and callback.get('data','').startswith('style:'):
                    style = callback['data'][6:]
                    if style in STYLES:
                        pending[user['id']] = style
                        send(chat,'Напиши тему ролика одним сообщением. Например: «Почему мы зеваем».')
                    continue
                body = message.get('text','').strip()
                if body in ('/start','/new','/help'):
                    send(chat,'🎬 Выбери формат ролика, потом отправь тему. Готовый MP4 пришлю сюда.',
                         [[{'text':'🎭 История','callback_data':'style:story'},
                           {'text':'⚡ Факт','callback_data':'style:fact'}],
                          [{'text':'📋 Подборка','callback_data':'style:list'}]])
                elif body and user['id'] in pending:
                    style = pending.pop(user['id'])
                    try:
                        JOBS.put_nowait((chat, style, body[:300]))
                        send(chat, f'Задача в очереди. Перед ней: {JOBS.qsize()-1}.')
                    except queue.Full:
                        send(chat, 'Очередь заполнена. Попробуй чуть позже.')
                elif body:
                    send(chat,'Нажми /new и выбери стиль.')
        except Exception as e:
            print('Polling error:',repr(e),flush=True)
            time.sleep(5)

if __name__ == '__main__':
    main()
