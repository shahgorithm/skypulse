from django.shortcuts import render
from django.http import JsonResponse
import requests
import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

# ── API Keys (set these as environment variables) ─────────────────
ACCUWEATHER_KEY = os.environ.get('ACCUWEATHER_KEY', '')
PEXELS_KEY      = os.environ.get('PEXELS_KEY', '')
OWM_KEY         = os.environ.get('OWM_KEY', '')
GROQ_KEY        = os.environ.get('GROQ_KEY', '')
# ─────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════
#  AccuWeather helpers
# ══════════════════════════════════════════════════════════════════

def _ow_icon(n, day):
    s = 'd' if day else 'n'
    if   n <= 5:           return f'01{s}', 'Clear'
    elif n <= 8:           return f'02{s}', 'Clouds'
    elif n == 11:          return f'50{s}', 'Fog'
    elif n in (12,13,14):  return f'10{s}', 'Rain'
    elif n in (15,16,17):  return f'11{s}', 'Thunderstorm'
    elif n == 18:          return f'09{s}', 'Drizzle'
    elif n <= 29:          return f'13{s}', 'Snow'
    elif n == 30:          return f'01{s}', 'Clear'
    elif n <= 32:          return f'50{s}', 'Mist'
    elif n <= 38:          return f'04{s}', 'Clouds'
    elif n <= 40:          return f'10{s}', 'Rain'
    elif n <= 42:          return f'11{s}', 'Thunderstorm'
    else:                  return f'13{s}', 'Snow'


def _normalize(location, current):
    c       = current[0]
    icon, mc = _ow_icon(c.get('WeatherIcon', 1), c.get('IsDayTime', True))
    geo     = location.get('GeoPosition', {})
    temp    = c['Temperature']['Metric']['Value']
    feels   = c['RealFeelTemperature']['Metric']['Value']
    t_min   = round(temp - 2, 1)
    t_max   = round(temp + 2, 1)
    try:
        rng   = c['TemperatureSummary']['Past24HourRange']
        t_min = rng['Minimum']['Metric']['Value']
        t_max = rng['Maximum']['Metric']['Value']
    except (KeyError, TypeError):
        pass
    return {
        'name':    location.get('LocalizedName', ''),
        'sys':     {'country': location.get('Country', {}).get('ID', '')},
        'main':    {
            'temp':       round(temp, 1),
            'feels_like': round(feels, 1),
            'humidity':   c.get('RelativeHumidity', 0),
            'pressure':   round(c.get('Pressure', {}).get('Metric', {}).get('Value', 1013)),
            'temp_min':   round(t_min, 1),
            'temp_max':   round(t_max, 1),
        },
        'weather': [{'main': mc,
                     'description': c.get('WeatherText', '').lower(),
                     'icon': icon}],
        'wind':       {'speed': round(c.get('Wind', {}).get('Speed', {}).get('Metric', {}).get('Value', 0) / 3.6, 1)},
        'visibility': round(c.get('Visibility', {}).get('Metric', {}).get('Value', 10) * 1000),
        'clouds':     {'all': c.get('CloudCover', 0)},
        'uv_index':   c.get('UVIndex', 0),
        'uv_text':    c.get('UVIndexText', ''),
        '_lat':       geo.get('Latitude', 0),
        '_lon':       geo.get('Longitude', 0),
    }


def get_accuweather_data(city):
    lr   = requests.get(
        'http://dataservice.accuweather.com/locations/v1/cities/search',
        params={'apikey': ACCUWEATHER_KEY, 'q': city, 'language': 'en-us'},
        timeout=6,
    )
    locs = lr.json()
    if isinstance(locs, dict): raise ValueError(locs.get('Message', 'API error'))
    if not locs:               raise ValueError(f'City "{city}" not found')

    loc     = locs[0]
    cr      = requests.get(
        f'http://dataservice.accuweather.com/currentconditions/v1/{loc["Key"]}',
        params={'apikey': ACCUWEATHER_KEY, 'details': 'true'},
        timeout=6,
    )
    current = cr.json()
    if isinstance(current, dict): raise ValueError(current.get('Message', 'Data unavailable'))
    if not current:               raise ValueError('No weather data returned')
    return _normalize(loc, current)

# ══════════════════════════════════════════════════════════════════
#  OWM Free APIs
# ══════════════════════════════════════════════════════════════════

def get_aqi(lat, lon):
    try:
        d    = requests.get(
            'http://api.openweathermap.org/data/2.5/air_pollution',
            params={'lat': lat, 'lon': lon, 'appid': OWM_KEY}, timeout=4,
        ).json()
        comp = d['list'][0]['components']
        v    = d['list'][0]['main']['aqi']
        labels  = {1:'Good', 2:'Fair', 3:'Moderate', 4:'Unhealthy', 5:'Very Unhealthy'}
        colors  = {1:'success', 2:'info', 3:'warning', 4:'danger', 5:'danger'}
        advices = {
            1: 'Excellent air quality. Safe for all outdoor activities.',
            2: 'Air quality is acceptable for most people.',
            3: 'Moderate air quality. Sensitive groups should limit prolonged exertion outdoors.',
            4: 'Unhealthy. Wear an N95 mask outdoors and limit outdoor time.',
            5: 'Very Unhealthy. Stay indoors and use an air purifier.',
        }
        bar_pct = {1: 15, 2: 35, 3: 55, 4: 78, 5: 100}
        return {
            'aqi':    v, 'label': labels[v], 'color': colors[v],
            'advice': advices[v], 'bar':   bar_pct[v],
            'pm2_5':  round(comp.get('pm2_5', 0), 1),
            'pm10':   round(comp.get('pm10', 0), 1),
            'co':     round(comp.get('co', 0)),
            'no2':    round(comp.get('no2', 0), 1),
            'o3':     round(comp.get('o3', 0), 1),
        }
    except Exception:
        return None


def get_5day_forecast(lat, lon):
    try:
        raw   = requests.get(
            'http://api.openweathermap.org/data/2.5/forecast',
            params={'lat': lat, 'lon': lon, 'appid': OWM_KEY, 'units': 'metric'},
            timeout=5,
        ).json()
        today = datetime.date.today()
        days  = defaultdict(list)
        for item in raw.get('list', []):
            days[datetime.datetime.fromtimestamp(item['dt']).date()].append(item)

        result = []
        for date in sorted(days.keys()):
            if date < today or len(result) >= 5:
                continue
            items = days[date]
            temps  = [x['main']['temp'] for x in items]
            icons  = [x['weather'][0]['icon'] for x in items]
            conds  = [x['weather'][0]['main'] for x in items]
            result.append({
                'day':       'Today' if date == today else date.strftime('%a'),
                'date':      date.strftime('%d %b'),
                'date_iso':  date.isoformat(),
                'is_today':  date == today,
                'temp_min':  round(min(temps)),
                'temp_max':  round(max(temps)),
                'icon':      max(set(icons), key=icons.count),
                'condition': max(set(conds), key=conds.count),
                'pop':       max(round(x.get('pop', 0) * 100) for x in items),
                'humidity':  round(sum(x['main']['humidity'] for x in items) / len(items)),
                'wind':      round(sum(x['wind']['speed'] for x in items) / len(items), 1),
            })
        return result
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════
#  Pexels video
# ══════════════════════════════════════════════════════════════════

def get_city_video(city):
    try:
        r = requests.get(
            'https://api.pexels.com/videos/search',
            headers={'Authorization': PEXELS_KEY},
            params={'query': city + ' city', 'per_page': 5, 'orientation': 'landscape'},
            timeout=5,
        )
        for v in r.json().get('videos', []):
            mp4 = sorted(
                [f for f in v.get('video_files', []) if 'mp4' in f.get('file_type', '')],
                key=lambda f: f.get('width', 0) * f.get('height', 0), reverse=True,
            )
            if mp4:
                return mp4[0]['link']
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════════════════════════
#  Wave SVG path
# ══════════════════════════════════════════════════════════════════

def build_wave_path(temps, width=600, height=40, pad=8):
    n = len(temps)
    if n == 0:
        return 'M0 20 L600 20', 300, 20
    t_min   = min(temps)
    t_range = max(temps) - t_min or 1
    x_step  = width / (n - 1) if n > 1 else width
    pts = [
        (round(i * x_step), round(pad + (1 - (t - t_min) / t_range) * (height - 2 * pad)))
        for i, t in enumerate(temps)
    ]
    path = f'M {pts[0][0]} {pts[0][1]}'
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]; x1, y1 = pts[i]
        cp = round((x0 + x1) / 2)
        path += f' C {cp} {y0}, {cp} {y1}, {x1} {y1}'
    ai = min(3, n - 1)
    return path, pts[ai][0], pts[ai][1]

# ══════════════════════════════════════════════════════════════════
#  Smart rule-based logic generators
# ══════════════════════════════════════════════════════════════════

def generate_insights(data, aqi):
    temp  = data['main']['temp']
    hum   = data['main']['humidity']
    wind  = data['wind']['speed']
    cond  = data['weather'][0]['main'].lower()
    vis   = data.get('visibility', 10000)
    uv    = data.get('uv_index', 0)
    out   = []

    if temp >= 40:
        out.append({'icon':'fa-fire','sev':'danger','title':'Extreme Heat',
            'text':f'{temp}°C recorded. Stay indoors 11 AM-4 PM. Drink 3+ litres of water daily. Avoid direct sun exposure.'})
    elif temp >= 33:
        out.append({'icon':'fa-sun','sev':'warning','title':'Hot Weather',
            'text':'Stay hydrated, wear light breathable clothing. Avoid strenuous outdoor activity during peak afternoon hours.'})
    elif temp <= 5:
        out.append({'icon':'fa-snowflake','sev':'info','title':'Very Cold',
            'text':'Dress in warm insulating layers. Watch for black ice on roads and pavements.'})
    elif 18 <= temp <= 27:
        out.append({'icon':'fa-face-smile','sev':'success','title':'Comfortable Conditions',
            'text':'Ideal weather for outdoor activities, sightseeing, and exercise.'})

    if 'thunderstorm' in cond:
        out.append({'icon':'fa-bolt','sev':'danger','title':'Thunderstorm Risk',
            'text':'Stay indoors. Avoid trees, open fields and metal structures. Do not drive unless essential.'})
    elif 'rain' in cond or 'drizzle' in cond:
        out.append({'icon':'fa-umbrella','sev':'info','title':'Rainy Conditions',
            'text':'Carry an umbrella. Allow extra travel time. Watch for waterlogged roads and reduced visibility.'})
    elif 'snow' in cond:
        out.append({'icon':'fa-snowflake','sev':'info','title':'Snowfall',
            'text':'Roads may be slippery. Drive carefully, keep chains ready, and wear non-slip footwear.'})
    elif 'fog' in cond or 'mist' in cond:
        out.append({'icon':'fa-smog','sev':'warning','title':'Fog / Mist',
            'text':'Low visibility due to fog. Use headlights while driving and maintain safe distances.'})

    if wind >= 15:
        out.append({'icon':'fa-wind','sev':'danger','title':'Strong Winds',
            'text':f'{round(wind*3.6)} km/h winds. Drone flights unsafe. Secure loose outdoor objects. Not suitable for outdoor events.'})
    elif wind >= 8:
        out.append({'icon':'fa-wind','sev':'warning','title':'Moderate Winds',
            'text':f'{round(wind*3.6)} km/h winds. Avoid drone flights. Outdoor events may be affected.'})

    if hum >= 80:
        out.append({'icon':'fa-droplet','sev':'warning','title':'High Humidity',
            'text':f'{hum}% humidity makes it feel significantly hotter. Use AC indoors. Prefer breathable fabrics.'})
    elif hum <= 25:
        out.append({'icon':'fa-droplet-slash','sev':'warning','title':'Low Humidity',
            'text':'Very dry air. Apply moisturizer, drink extra water, and use a humidifier indoors.'})

    if uv >= 8:
        out.append({'icon':'fa-radiation','sev':'danger','title':f'High UV Index ({uv})',
            'text':'Extremely high UV radiation. Apply SPF 50+ sunscreen, wear UV-blocking sunglasses and a hat.'})
    elif uv >= 6:
        out.append({'icon':'fa-sun','sev':'warning','title':f'Elevated UV Index ({uv})',
            'text':'Use SPF 30+ sunscreen between 10 AM and 4 PM. Seek shade during peak hours.'})

    if vis < 1000:
        out.append({'icon':'fa-eye-slash','sev':'danger','title':'Very Low Visibility',
            'text':f'Only {vis} m visibility. Fog lights mandatory. Avoid driving if possible.'})

    if aqi:
        if aqi['aqi'] >= 4:
            out.append({'icon':'fa-lungs','sev':'danger','title':'Poor Air Quality',
                'text':'Unhealthy air. Wear an N95 mask outdoors. Keep windows closed and run an air purifier indoors.'})
        elif aqi['aqi'] == 3:
            out.append({'icon':'fa-lungs','sev':'warning','title':'Moderate Air Quality',
                'text':'Sensitive groups (asthma, children, elderly) should limit prolonged outdoor exposure.'})

    return out[:6]


def generate_alerts(data, aqi):
    temp  = data['main']['temp']
    wind  = data['wind']['speed']
    cond  = data['weather'][0]['main'].lower()
    vis   = data.get('visibility', 10000)
    hum   = data['main']['humidity']
    out   = []

    if 'thunderstorm' in cond:
        out.append({'c':'danger', 'i':'fa-triangle-exclamation', 't':'Thunderstorm Warning', 'm':'Dangerous electrical activity'})
    if temp >= 40:
        out.append({'c':'danger', 'i':'fa-temperature-high', 't':'Extreme Heat Alert', 'm':f'{temp}°C, heat emergency'})
    if wind >= 15:
        out.append({'c':'danger', 'i':'fa-wind', 't':'Storm Wind Alert', 'm':f'{round(wind*3.6)} km/h winds'})
    elif wind >= 10:
        out.append({'c':'warning', 'i':'fa-wind', 't':'High Wind Advisory', 'm':f'{round(wind*3.6)} km/h winds'})
    if 'rain' in cond or 'drizzle' in cond:
        out.append({'c':'info', 'i':'fa-cloud-rain', 't':'Rain Alert', 'm':'Precipitation expected'})
    if vis < 1000:
        out.append({'c':'warning', 'i':'fa-eye-slash', 't':'Low Visibility Alert', 'm':f'{vis} m visibility'})
    if aqi and aqi['aqi'] >= 4:
        out.append({'c':'danger', 'i':'fa-lungs', 't':'Air Quality Alert', 'm':f'AQI: {aqi["label"]}'})
    if hum >= 85 and temp >= 30:
        out.append({'c':'warning', 'i':'fa-droplet', 't':'Extreme Humidity', 'm':f'{hum}% humidity + {temp}°C'})
    return out


def generate_activities(data):
    temp  = data['main']['temp']
    wind  = data['wind']['speed']
    cond  = data['weather'][0]['main'].lower()

    if 'thunderstorm' in cond:
        return {
            'outdoor': [],
            'indoor':  [
                {'icon':'fa-gamepad','name':'Board Games / Video Games','ok':True},
                {'icon':'fa-film',   'name':'Movie Marathon',           'ok':True},
                {'icon':'fa-book',   'name':'Reading',                  'ok':True},
                {'icon':'fa-mug-hot','name':'Hot Beverage & Relax',     'ok':True},
                {'icon':'fa-music',  'name':'Music / Instruments',      'ok':True},
            ],
        }
    if 'rain' in cond or 'drizzle' in cond:
        return {
            'outdoor': [
                {'icon':'fa-camera',        'name':'Rain Photography',       'ok':True},
                {'icon':'fa-person-walking','name':'Walk with Umbrella',      'ok':True},
            ],
            'indoor': [
                {'icon':'fa-mug-hot',  'name':'Café Visit',       'ok':True},
                {'icon':'fa-film',     'name':'Cinema / Streaming','ok':True},
                {'icon':'fa-book',     'name':'Reading',           'ok':True},
                {'icon':'fa-utensils', 'name':'Home Cooking/Baking','ok':True},
                {'icon':'fa-dumbbell', 'name':'Indoor Gym',        'ok':True},
            ],
        }
    if 'snow' in cond:
        return {
            'outdoor': [
                {'icon':'fa-person-skiing','name':'Skiing / Snowboard','ok':True},
                {'icon':'fa-snowman',      'name':'Build a Snowman',   'ok':True},
                {'icon':'fa-camera',       'name':'Snow Photography',  'ok':True},
            ],
            'indoor': [
                {'icon':'fa-mug-hot','name':'Hot Chocolate & Relax','ok':True},
                {'icon':'fa-fire',   'name':'Fireplace & Movie',    'ok':True},
            ],
        }
    if 'clear' in cond:
        return {
            'outdoor': [
                {'icon':'fa-bicycle',         'name':'Cycling',             'ok': temp < 38},
                {'icon':'fa-camera',          'name':'Outdoor Photography', 'ok':True},
                {'icon':'fa-person-walking',  'name':'Morning Walk / Jog',  'ok': temp < 36},
                {'icon':'fa-person-hiking',   'name':'Hiking',              'ok': 10 < temp < 35 and wind < 10},
                {'icon':'fa-umbrella-beach',  'name':'Beach Visit',         'ok': temp > 25},
                {'icon':'fa-futbol',          'name':'Outdoor Sports',      'ok': 15 < temp < 35 and wind < 12},
            ],
            'indoor': [
                {'icon':'fa-dumbbell',  'name':'Gym',              'ok':True},
                {'icon':'fa-mug-hot',   'name':'Café Work Session', 'ok':True},
            ],
        }
    return {
        'outdoor': [
            {'icon':'fa-camera',       'name':'Photography (soft light)','ok':True},
            {'icon':'fa-person-walking','name':'Walk / Jog',             'ok':True},
            {'icon':'fa-bicycle',       'name':'Cycling',                'ok': wind < 12},
        ],
        'indoor': [
            {'icon':'fa-landmark', 'name':'Museum Visit','ok':True},
            {'icon':'fa-bag-shopping','name':'Shopping',  'ok':True},
            {'icon':'fa-dumbbell', 'name':'Gym',          'ok':True},
            {'icon':'fa-film',     'name':'Cinema',       'ok':True},
        ],
    }


def generate_b2b(data):
    temp  = data['main']['temp']
    wind  = data['wind']['speed']
    cond  = data['weather'][0]['main'].lower()
    rain  = 'rain' in cond or 'drizzle' in cond
    storm = 'thunderstorm' in cond
    out   = []

    if storm:
        out.append({'sector':'Delivery / Logistics','icon':'fa-truck','c':'danger','impact':'High',
            'text':'Severe weather. Major delivery disruptions expected. Consider postponing non-urgent shipments.'})
    elif rain:
        out.append({'sector':'Delivery / Logistics','icon':'fa-truck','c':'warning','impact':'Medium',
            'text':'Rain may slow delivery operations by 20-40%. Inform customers of possible delays.'})
    else:
        out.append({'sector':'Delivery / Logistics','icon':'fa-truck','c':'success','impact':'Low',
            'text':'Clear conditions, optimal for delivery and logistics operations.'})

    if wind >= 10 or storm:
        out.append({'sector':'Drone Operations','icon':'fa-helicopter','c':'danger','impact':'High',
            'text':f'Winds at {round(wind*3.6)} km/h. Drone flights not recommended. High crash and data loss risk.'})
    elif wind >= 6:
        out.append({'sector':'Drone Operations','icon':'fa-helicopter','c':'warning','impact':'Medium',
            'text':'Moderate wind. Professional drones only. Monitor gusts carefully.'})
    else:
        out.append({'sector':'Drone Operations','icon':'fa-helicopter','c':'success','impact':'Low',
            'text':'Favourable conditions for drone operations.'})

    if storm or wind >= 12:
        out.append({'sector':'Construction','icon':'fa-helmet-safety','c':'danger','impact':'High',
            'text':'Halt all outdoor construction. High risk of falling materials and worker injury.'})
    elif rain:
        out.append({'sector':'Construction','icon':'fa-helmet-safety','c':'warning','impact':'Medium',
            'text':'Exterior work may be delayed. Protect materials and equipment from moisture.'})
    else:
        out.append({'sector':'Construction','icon':'fa-helmet-safety','c':'success','impact':'Low',
            'text':'Good conditions for outdoor construction activities.'})

    if rain or storm:
        out.append({'sector':'Retail / E-commerce','icon':'fa-store','c':'success','impact':'Positive',
            'text':'Rain drives online purchases. Umbrella, delivery and comfort product demand is up.'})
    elif temp >= 35:
        out.append({'sector':'Food & Beverage','icon':'fa-store','c':'success','impact':'Positive',
            'text':'Hot weather sharply boosts cold drinks, ice cream, and chilled beverage sales.'})
    else:
        out.append({'sector':'Tourism / Hospitality','icon':'fa-hotel','c':'success','impact':'Positive',
            'text':'Pleasant conditions. Expect higher footfall at restaurants, parks, and attractions.'})

    return out[:4]


def generate_social(data):
    cond = data['weather'][0]['main'].lower()
    if 'clear' in cond:
        return [
            {'platform':'Instagram','icon':'fa-instagram','color':'#E1306C','time':'5:30-7:00 PM',
             'type':'Golden Hour / Sunset Shots','tip':'Clear skies create vivid golden hour lighting, perfect for travel, lifestyle and nature content.'},
            {'platform':'TikTok','icon':'fa-tiktok','color':'#010101','time':'4:00-6:00 PM',
             'type':'Outdoor Day-in-Life Vlog','tip':'Day-in-life and sunny day POV content gets highest TikTok engagement on clear days.'},
            {'platform':'YouTube','icon':'fa-youtube','color':'#FF0000','time':'Morning',
             'type':'Outdoor Tutorial / Travel','tip':'Film outdoor tutorials or city walk-throughs in clear bright daylight for best video quality.'},
            {'platform':'Facebook','icon':'fa-facebook','color':'#1877F2','time':'12-2 PM',
             'type':'Local Events / Attractions','tip':'Share outdoor events and local spots. Clear-weather posts boost Facebook page reach.'},
        ]
    if 'rain' in cond or 'drizzle' in cond:
        return [
            {'platform':'Instagram','icon':'fa-instagram','color':'#E1306C','time':'After 6 PM',
             'type':'Cozy Indoor Aesthetic','tip':'Coffee, books, and candle shots generate high saves on Instagram during rain.'},
            {'platform':'YouTube','icon':'fa-youtube','color':'#FF0000','time':'Any time',
             'type':'Rain ASMR / Study With Me','tip':'Rain-sounds increase viewer retention, ideal for Lo-fi study vlogs.'},
            {'platform':'Twitter/X','icon':'fa-x-twitter','color':'#000','time':'8-10 AM',
             'type':'Weather Commentary','tip':'Rainy day commute and coffee tweets are highly relatable and go viral easily.'},
        ]
    if 'thunderstorm' in cond:
        return [
            {'platform':'Instagram','icon':'fa-instagram','color':'#E1306C','time':'During storm',
             'type':'Dramatic Storm Photography','tip':'Lightning and storm sky shots are among the most shared weather content.'},
            {'platform':'Twitter/X','icon':'fa-x-twitter','color':'#000','time':'During storm',
             'type':'Safety Awareness Updates','tip':'Safety posts about active storms get maximum reach and community shares.'},
        ]
    if 'snow' in cond:
        return [
            {'platform':'Instagram','icon':'fa-instagram','color':'#E1306C','time':'After fresh snowfall',
             'type':'Winter Landscape Photography','tip':'Fresh snow creates magical visuals. Extremely high save rate on Instagram.'},
            {'platform':'TikTok','icon':'fa-tiktok','color':'#010101','time':'Afternoon',
             'type':'Snow Day POV / Reaction','tip':'First-snow reactions and snowfall timelapses are high-engagement TikTok content.'},
        ]
    return [
        {'platform':'Instagram','icon':'fa-instagram','color':'#E1306C','time':'9-11 AM',
         'type':'Moody / Portrait Shots','tip':'Overcast diffused light eliminates harsh shadows, ideal for fashion and portrait photography.'},
        {'platform':'LinkedIn','icon':'fa-linkedin','color':'#0077B5','time':'10 AM-12 PM',
         'type':'Professional Video / Blog Posts','tip':'Cloudy days are ideal for filming professional indoor content and writing thought-leadership articles.'},
    ]


def get_travel_rec(city, month, data):
    temp      = data['main']['temp']
    cond      = data['weather'][0]['main'].lower()
    hum       = data['main']['humidity']
    m_name    = datetime.date(2000, month, 1).strftime('%B')

    if 15 <= temp <= 28 and 'thunder' not in cond and 'rain' not in cond and hum < 72:
        r, rc, rt = 'Excellent', 'success', 'Perfect time to visit'
    elif 8 <= temp <= 35 and 'thunderstorm' not in cond:
        r, rc, rt = 'Good', 'info', 'Good conditions for travel'
    elif 'thunderstorm' in cond or temp > 42 or temp < -5:
        r, rc, rt = 'Poor', 'danger', 'Extreme conditions, not recommended'
    else:
        r, rc, rt = 'Fair', 'warning', 'Manageable but not ideal'

    if temp > 35:
        best = 'November to February'
        reason = 'Cooler winter months offer the most comfortable sightseeing conditions.'
    elif temp < 10:
        best = 'April to June and September to October'
        reason = 'Mild spring and autumn months provide the best travel experience.'
    elif 'rain' in cond:
        best = 'Dry season months, check local forecast'
        reason = 'Avoid monsoon or wet season for a more pleasant visit.'
    else:
        best = f'{m_name} (currently comfortable)'
        reason = 'You are visiting during a pleasant period of the year.'

    tips = []
    if temp > 35:     tips.append('Pack SPF 50+ sunscreen and stay hydrated throughout the day')
    if 'rain' in cond: tips.append('Bring waterproof clothing, shoes, and a compact umbrella')
    if hum > 75:       tips.append('Choose lightweight, sweat-wicking fabrics for comfort')
    if temp < 10:      tips.append('Warm layers, a winter jacket and thermal innerwear are essential')
    tips.append('Check local festivals and public holidays before booking')
    tips.append('Book accommodation early during peak tourist months')

    return {'rating': r, 'rc': rc, 'rt': rt, 'best': best, 'reason': reason,
            'tips': tips, 'city': city}

# ══════════════════════════════════════════════════════════════════
#  NEW: Weather Impact Score
# ══════════════════════════════════════════════════════════════════

def generate_impact_score(data, aqi, forecast):
    temp  = data['main']['temp']
    wind  = data['wind']['speed']
    hum   = data['main']['humidity']
    cond  = data['weather'][0]['main'].lower()
    uv    = data.get('uv_index', 0)
    vis   = data.get('visibility', 10000)

    def _label(s):
        if s >= 8: return 'Excellent', 'success'
        elif s >= 6: return 'Good', 'info'
        elif s >= 4: return 'Fair', 'warning'
        else:       return 'Poor', 'danger'

    # Outdoor score
    o = 10
    if temp > 40:              o -= 4
    elif temp > 35:            o -= 2
    elif temp < 5:             o -= 3
    if wind > 15:              o -= 3
    elif wind > 10:            o -= 1
    if 'thunderstorm' in cond: o -= 5
    elif 'rain' in cond:       o -= 2
    if hum > 85:               o -= 1
    if uv > 8:                 o -= 1
    o = max(0, min(10, o))

    # Travel score
    t = 10
    if 'thunderstorm' in cond:          t -= 5
    elif 'rain' in cond or 'snow' in cond: t -= 2
    if vis < 1000:                      t -= 3
    elif vis < 3000:                    t -= 1
    if wind > 15:                       t -= 2
    if temp > 42 or temp < -5:          t -= 3
    t = max(0, min(10, t))

    # Sports score
    sp = 10
    if 'thunderstorm' in cond:         sp -= 6
    elif 'rain' in cond:               sp -= 3
    if wind > 12:                      sp -= 2
    if temp > 38:                      sp -= 3
    elif temp < 0:                     sp -= 3
    if hum > 85 and temp > 28:         sp -= 2
    sp = max(0, min(10, sp))

    # Air quality score
    if aqi:
        aq = {1: 10, 2: 8, 3: 6, 4: 3, 5: 1}.get(aqi['aqi'], 5)
    else:
        aq = 5

    overall = round((o + t + sp + aq) / 4, 1)
    ol, oc = _label(int(overall))

    return {
        'outdoor': {'score': o,  'label': _label(o)[0],  'color': _label(o)[1],  'pct': o * 10},
        'travel':  {'score': t,  'label': _label(t)[0],  'color': _label(t)[1],  'pct': t * 10},
        'sports':  {'score': sp, 'label': _label(sp)[0], 'color': _label(sp)[1], 'pct': sp * 10},
        'air':     {'score': aq, 'label': _label(aq)[0], 'color': _label(aq)[1], 'pct': aq * 10},
        'overall': overall, 'overall_label': ol, 'overall_color': oc,
    }

# ══════════════════════════════════════════════════════════════════
#  NEW: Weather Mood Mode
# ══════════════════════════════════════════════════════════════════

def generate_mood(data):
    temp = data['main']['temp']
    cond = data['weather'][0]['main'].lower()
    hum  = data['main']['humidity']

    if 'thunderstorm' in cond:
        return {'mood': 'Dramatic & Intense', 'emoji': '⛈', 'color': '#ff8f8f',
                'bg': 'rgba(220,53,69,.15)',
                'desc': 'Wild electric energy in the air. Perfect for deep focus, dramatic music, or creative writing.',
                'activities': ['Write poetry or a short story', 'Storm photography through window', 'Deep focus music session', 'Watch a thriller film'],
                'vibe': 'danger'}
    if 'snow' in cond:
        return {'mood': 'Magical & Cozy', 'emoji': '❄', 'color': '#a8d8ff',
                'bg': 'rgba(13,202,240,.12)',
                'desc': 'Soft silent snowfall transforms the world into a peaceful wonderland.',
                'activities': ['Hot chocolate & blanket', 'Snowfall photography', 'Reading by the window', 'Slow cooking'],
                'vibe': 'info'}
    if 'rain' in cond or 'drizzle' in cond:
        return {'mood': 'Cozy Rainy Vibes', 'emoji': '🌧', 'color': '#50daff',
                'bg': 'rgba(13,202,240,.10)',
                'desc': 'Gentle rain creates a calming white-noise backdrop ideal for introspection and creativity.',
                'activities': ['Book & coffee session', 'Journaling or writing', 'Lo-fi / indie music', 'ASMR & productivity work'],
                'vibe': 'info'}
    if 'fog' in cond or 'mist' in cond:
        return {'mood': 'Mysterious & Calm', 'emoji': '🌫', 'color': '#b0c4d8',
                'bg': 'rgba(255,255,255,.07)',
                'desc': 'The fog creates a dreamlike atmosphere, perfect for slow, mindful activities.',
                'activities': ['Yoga & meditation', 'Contemplative writing', 'Moody photography', 'Mindful nature walk'],
                'vibe': 'secondary'}
    if 'clear' in cond and temp > 30:
        return {'mood': 'Energetic & Sunny', 'emoji': '☀', 'color': '#ffd060',
                'bg': 'rgba(255,193,7,.12)',
                'desc': 'Bright sunshine energises the mind and body. Great for outdoor adventures and social activities.',
                'activities': ['Morning jog or gym', 'Beach or pool outing', 'Outdoor café work session', 'Socialise with friends'],
                'vibe': 'warning'}
    if 'clear' in cond:
        return {'mood': 'Vibrant & Joyful', 'emoji': '🌤', 'color': '#6fe0a8',
                'bg': 'rgba(25,135,84,.12)',
                'desc': 'Perfect clear sky lifts spirits and boosts motivation for any type of activity.',
                'activities': ['Outdoor exercise', 'Golden hour photography', 'Picnic or park walk', 'Productive work session'],
                'vibe': 'success'}
    if temp < 10:
        return {'mood': 'Crisp & Refreshing', 'emoji': '🧥', 'color': '#a0c8ff',
                'bg': 'rgba(13,202,240,.10)',
                'desc': 'Brisk cool air sharpens the mind and creates perfect conditions for deep focused work.',
                'activities': ['Deep work or study session', 'Hot soup & tea', 'Museum or gallery visit', 'Board games evening'],
                'vibe': 'info'}
    return {'mood': 'Relaxed & Mellow', 'emoji': '☁', 'color': '#c0c0d0',
            'bg': 'rgba(255,255,255,.07)',
            'desc': 'Soft overcast skies create a diffused, peaceful atmosphere ideal for low-stress activities.',
            'activities': ['Slow work session', 'Nature walk', 'Portrait photography', 'Home cooking'],
            'vibe': 'secondary'}

# ══════════════════════════════════════════════════════════════════
#  NEW: Content Creator Tool
# ══════════════════════════════════════════════════════════════════

def generate_content_creator(data):
    temp  = data['main']['temp']
    cond  = data['weather'][0]['main'].lower()
    wind  = data['wind']['speed']
    vis   = data.get('visibility', 10000)
    uv    = data.get('uv_index', 0)

    if 'clear' in cond:
        lighting = 'Golden Hour Optimal'; lc = 'success'
        ldesc = 'Clear skies produce stunning golden hour light. Shoot 30-60 min before sunset for best results.'
    elif 'cloud' in cond:
        lighting = 'Diffused, Perfect for Portraits'; lc = 'info'
        ldesc = 'Overcast clouds act as a giant natural softbox, eliminates harsh shadows, ideal for portraits & products.'
    elif 'rain' in cond or 'drizzle' in cond:
        lighting = 'Low Light, Long Exposure'; lc = 'warning'
        ldesc = 'Rain reflections & puddles create surreal cityscapes. Use long exposure or high ISO techniques.'
    elif 'fog' in cond or 'mist' in cond:
        lighting = 'Atmospheric / Cinematic'; lc = 'info'
        ldesc = 'Fog adds dramatic depth and mystery. Backlight through mist creates cinematic quality frames.'
    else:
        lighting = 'Variable / Mixed'; lc = 'secondary'
        ldesc = 'Mixed lighting conditions. Scout your location and meter carefully before shooting.'

    if 'thunderstorm' in cond:
        outdoor = 'Dangerous, Avoid Outdoors'; oc = 'danger'
    elif vis < 2000 or wind > 15:
        outdoor = 'Challenging Conditions'; oc = 'warning'
    elif 'rain' in cond:
        outdoor = 'Possible (Rain Gear Required)'; oc = 'warning'
    elif temp > 40 or uv > 8:
        outdoor = 'Possible (Protect Equipment)'; oc = 'warning'
    elif 15 <= temp <= 32 and wind < 10:
        outdoor = 'Excellent'; oc = 'success'
    else:
        outdoor = 'Good'; oc = 'info'

    if 'clear' in cond:
        shots = ['Golden hour landscape & silhouettes', 'Blue hour cityscapes', 'Street photography with sharp shadows', 'Travel & lifestyle portraits']
    elif 'rain' in cond or 'drizzle' in cond:
        shots = ['Rain reflections on wet streets', 'Umbrella bokeh portraits', 'Window droplet macro', 'Moody long-exposure cityscape']
    elif 'snow' in cond:
        shots = ['Snow landscape long exposure', 'Snowfall bokeh portraits', 'Frozen nature macro', 'Winter street documentary']
    elif 'fog' in cond or 'mist' in cond:
        shots = ['Silhouettes in morning fog', 'Architectural abstraction through haze', 'Backlit fog low-angle shots', 'Street lamps in mist']
    elif 'cloud' in cond:
        shots = ['Soft-light portrait sessions', 'Flat-lay product photography', 'Cloud formation wide-angle', 'Documentary street work']
    else:
        shots = ['Outdoor lifestyle editorial', 'Environmental portraits', 'Architecture & urban geometry', 'Street candids']

    gear = []
    if 'rain' in cond or 'fog' in cond:
        gear.append('Camera rain cover / waterproof bag essential')
    if temp < 5:
        gear.append('Extra batteries: cold drains power significantly faster')
    if 'clear' in cond and uv > 6:
        gear.append('Polarising filter cuts glare and intensifies sky contrast')
    if 'rain' in cond or 'cloud' in cond:
        gear.append('ND filter for long exposure in low and variable light')
    gear.append('Lens cloth: essential in every shooting condition')
    gear.append('Golden hour timer app for precise shoot timing')

    return {
        'lighting': lighting, 'lc': lc, 'ldesc': ldesc,
        'outdoor': outdoor, 'oc': oc,
        'shots': shots[:4], 'gear': gear[:4],
    }

# ══════════════════════════════════════════════════════════════════
#  NEW: City Comparison
# ══════════════════════════════════════════════════════════════════

def get_comparison_data(cities):
    results = []
    for city_name in cities[:4]:
        try:
            d = get_accuweather_data(city_name)
            d.pop('_lat', None)
            d.pop('_lon', None)
            results.append({'city': city_name.title(), 'data': d, 'error': None})
        except Exception as e:
            results.append({'city': city_name.title(), 'data': None, 'error': str(e)})
    return results

# ══════════════════════════════════════════════════════════════════
#  Daily Lifestyle Recommendations
# ══════════════════════════════════════════════════════════════════

def get_lifestyle_recommendations(data, aqi_data, city):
    now  = datetime.datetime.now()
    hour = now.hour

    if   5  <= hour < 12: greeting, g_icon = 'Good morning',  'fa-sun'
    elif 12 <= hour < 17: greeting, g_icon = 'Good afternoon','fa-cloud-sun'
    elif 17 <= hour < 21: greeting, g_icon = 'Good evening',  'fa-moon'
    else:                 greeting, g_icon = 'Good night',     'fa-star'

    temp      = data['main']['temp']
    feels     = data['main']['feels_like']
    humidity  = data['main']['humidity']
    wind_kmh  = data['wind']['speed'] * 3.6
    condition = data['weather'][0]['main'].lower()
    vis_km    = data.get('visibility', 10000) / 1000
    aqi       = aqi_data['aqi'] if aqi_data else None

    recs = []

    # 1. Workout
    if 'thunderstorm' in condition:
        recs.append({'icon':'fa-house','text':'Thunderstorm active — indoor workout or gym only today','tag':'Avoid Outdoors','c':'danger'})
    elif temp > 38:
        recs.append({'icon':'fa-person-running','text':f'Best outdoor workout: 5:30–7:00 AM before heat peaks ({temp:.0f}°C now)','tag':'Early Morning Only','c':'warning'})
    elif temp > 32:
        recs.append({'icon':'fa-person-running','text':'Best outdoor workout windows: 6:00–8:00 AM or after 7:00 PM','tag':'Morning / Evening','c':'warning'})
    elif 15 <= temp <= 28 and 'rain' not in condition and 'snow' not in condition:
        recs.append({'icon':'fa-person-running','text':'Excellent conditions for outdoor activity — lace up and go!','tag':'All Day','c':'success'})
    elif temp < 5:
        recs.append({'icon':'fa-person-running','text':f'Very cold at {temp:.0f}°C — warm up indoors first, thermal layers outside','tag':'Layer Up','c':'info'})
    elif 'rain' in condition or 'drizzle' in condition:
        recs.append({'icon':'fa-person-running','text':'Rainy — indoor workout recommended or run between showers','tag':'Indoor Preferred','c':'info'})
    else:
        recs.append({'icon':'fa-person-running','text':'Decent conditions for a morning or evening outdoor session','tag':'Active Day','c':'success'})

    # 2. Leisure
    if 'thunderstorm' in condition:
        recs.append({'icon':'fa-couch','text':'Perfect day for movies, reading, or indoor hobbies at home','tag':'Stay In','c':'info'})
    elif 'rain' in condition or 'drizzle' in condition:
        recs.append({'icon':'fa-mug-hot','text':'Great day for a cosy café, museum, or mall visit','tag':'Indoor','c':'info'})
    elif temp > 35:
        recs.append({'icon':'fa-umbrella-beach','text':'Beat the heat — pool, AC café, or an evening beach outing','tag':'Heat Advisory','c':'warning'})
    elif 20 <= temp <= 32 and 'rain' not in condition:
        recs.append({'icon':'fa-umbrella-beach','text':'Ideal for an outdoor café, park stroll, or a seaside visit','tag':'Great Outdoors','c':'success'})
    elif temp < 10:
        recs.append({'icon':'fa-mug-hot','text':'Cosy café indoors or a scenic winter drive','tag':'Cosy Day','c':'info'})
    else:
        recs.append({'icon':'fa-person-walking','text':'Pleasant weather for a light walk or some window shopping','tag':'Light Activity','c':'success'})

    # 3. Photography
    if 'clear' in condition:
        recs.append({'icon':'fa-camera','text':'Shoot golden hour 30–60 min before sunset for stunning warm light','tag':'Golden Hour','c':'success'})
    elif 'cloud' in condition:
        recs.append({'icon':'fa-camera','text':'Overcast gives soft diffused light — perfect for portraits all day','tag':'Soft Light','c':'info'})
    elif 'rain' in condition or 'drizzle' in condition:
        recs.append({'icon':'fa-camera','text':'Capture rain reflections and moody skies — waterproof your gear','tag':'Moody Shots','c':'warning'})
    elif 'fog' in condition or 'mist' in condition:
        recs.append({'icon':'fa-camera','text':'Foggy depth and silhouettes — great for atmospheric photography','tag':'Atmospheric','c':'info'})
    else:
        recs.append({'icon':'fa-camera','text':'Check golden hour timing for the best natural light shots','tag':'Plan Ahead','c':'info'})

    # 4. Hydration
    if temp > 35 or (temp > 28 and humidity > 65):
        recs.append({'icon':'fa-droplet','text':f'High heat index ({feels:.0f}°C feels like) — drink at least 3–4L water today','tag':'Drink 3L+','c':'danger'})
    elif temp > 25:
        recs.append({'icon':'fa-droplet','text':'Warm day — aim for 2.5L and carry a bottle when going out','tag':'Drink 2.5L','c':'warning'})
    else:
        recs.append({'icon':'fa-droplet','text':'Stay hydrated throughout the day — your goal is 2L of water','tag':'Drink 2L','c':'info'})

    # 5. Commute
    if 'thunderstorm' in condition:
        recs.append({'icon':'fa-car','text':'Delay non-essential travel — active thunderstorm on roads','tag':'Delay Travel','c':'danger'})
    elif wind_kmh > 55:
        recs.append({'icon':'fa-car','text':f'Strong winds {wind_kmh:.0f} km/h — grip the wheel and watch for debris','tag':'High Winds','c':'warning'})
    elif 'fog' in condition or 'mist' in condition:
        recs.append({'icon':'fa-car','text':'Low visibility — use fog lights and reduce speed significantly','tag':'Foggy Roads','c':'warning'})
    elif 'rain' in condition or 'drizzle' in condition:
        recs.append({'icon':'fa-car','text':'Wet roads — brake early, maintain extra following distance','tag':'Slippery','c':'warning'})
    elif 'snow' in condition:
        recs.append({'icon':'fa-car','text':'Icy roads possible — drive slowly and avoid sudden braking','tag':'Icy Roads','c':'danger'})
    else:
        recs.append({'icon':'fa-car','text':f'Clear driving conditions — good visibility at {vis_km:.0f} km','tag':'Clear Roads','c':'success'})

    # 6. Clothing / AQI
    if aqi and aqi >= 4:
        recs.append({'icon':'fa-wind','text':'Very poor air quality — wear an N95 mask outdoors, close windows','tag':'AQI Alert','c':'danger'})
    elif aqi and aqi == 3:
        recs.append({'icon':'fa-wind','text':'Moderate air quality — limit prolonged outdoor exposure today','tag':'Moderate AQI','c':'warning'})
    elif temp > 35:
        recs.append({'icon':'fa-shirt','text':'Light breathable cotton/linen — avoid dark colours in direct sun','tag':'Light Wear','c':'warning'})
    elif temp > 25:
        recs.append({'icon':'fa-shirt','text':'Light summer clothes, sunglasses, and SPF 50 sunscreen','tag':'Summer Wear','c':'info'})
    elif temp > 15:
        recs.append({'icon':'fa-shirt','text':'Light jacket or cardigan — easy layering day','tag':'Light Jacket','c':'info'})
    elif temp > 5:
        recs.append({'icon':'fa-shirt','text':'Warm jacket and layers — chilly outside today','tag':'Warm Up','c':'info'})
    else:
        recs.append({'icon':'fa-shirt','text':'Heavy winter coat, thermal layers, gloves and hat needed','tag':'Bundle Up','c':'warning'})

    return {
        'greeting':   greeting,
        'g_icon':     g_icon,
        'date_str':   now.strftime('%B %d, %Y'),
        'day_str':    now.strftime('%A'),
        'recs':       recs,
    }


# ══════════════════════════════════════════════════════════════════
#  Main view
# ══════════════════════════════════════════════════════════════════

def home(request):
    city  = request.POST.get('city', 'karachi') if request.method == 'POST' else 'karachi'

    data  = None
    error = None
    lat = lon = 0

    try:
        data = get_accuweather_data(city)
        lat  = data.pop('_lat', 0)
        lon  = data.pop('_lon', 0)
    except ValueError as e:
        error = str(e)
    except Exception:
        error = 'Weather service temporarily unavailable. Please try again.'

    # Parallel: AQI + forecast + video
    aqi_data   = None
    forecast   = []
    city_video = None

    def _aqi():  return get_aqi(lat, lon) if lat else None
    def _fc():   return get_5day_forecast(lat, lon) if lat else []
    def _vid():  return get_city_video(city)

    with ThreadPoolExecutor(max_workers=3) as ex:
        fa = ex.submit(_aqi)
        ff = ex.submit(_fc)
        fv = ex.submit(_vid)
        try: aqi_data   = fa.result(timeout=5)
        except: pass
        try: forecast   = ff.result(timeout=6)
        except: pass
        try: city_video = fv.result(timeout=7)
        except: pass

    # Logic generators
    insights   = generate_insights(data, aqi_data)  if data else []
    alerts     = generate_alerts(data, aqi_data)    if data else []
    activities = generate_activities(data)           if data else {'outdoor': [], 'indoor': []}
    b2b        = generate_b2b(data)                 if data else []
    social     = generate_social(data)              if data else []
    travel     = get_travel_rec(city, datetime.date.today().month, data) if data else None

    # New generators
    impact     = generate_impact_score(data, aqi_data, forecast) if data else None
    mood       = generate_mood(data)                              if data else None
    creator    = generate_content_creator(data)                   if data else None
    lifestyle  = get_lifestyle_recommendations(data, aqi_data, city) if data else None

    # Event Weather Planner
    event_date     = request.POST.get('event_date', '') if request.method == 'POST' else ''
    event_forecast = None
    if event_date and forecast:
        for f in forecast:
            if f.get('date_iso') == event_date:
                event_forecast = f
                # Compute event risk
                pop   = f.get('pop', 0)
                wind  = f.get('wind', 0)
                cond  = f.get('condition', '').lower()
                if 'thunderstorm' in cond or pop > 80 or wind > 12:
                    f['risk'] = 'High'; f['risk_c'] = 'danger'
                elif 'rain' in cond or pop > 40 or wind > 8:
                    f['risk'] = 'Moderate'; f['risk_c'] = 'warning'
                else:
                    f['risk'] = 'Low'; f['risk_c'] = 'success'
                break

    # City Comparison
    compare_input = request.POST.get('compare_cities', '') if request.method == 'POST' else ''
    comparison = []
    if compare_input:
        city_list = [c.strip() for c in compare_input.replace(';', ',').split(',') if c.strip()]
        comparison = get_comparison_data(city_list)

    # Forecast chart data (for Historical Trends / Chart.js)
    chart_labels  = [f['day'] for f in forecast]
    chart_max     = [f['temp_max'] for f in forecast]
    chart_min     = [f['temp_min'] for f in forecast]
    chart_hum     = [f['humidity'] for f in forecast]
    chart_wind    = [f['wind'] for f in forecast]
    chart_pop     = [f['pop'] for f in forecast]

    # Forecast strip
    hourly_temps  = []
    wave_path     = ''
    wave_active_x = 300
    wave_active_y = 20

    if data:
        t_min = round(data['main']['temp_min'])
        t_max = round(data['main']['temp_max'])
        t_cur = round(data['main']['temp'])
        hourly_temps = [
            {'time': '06:00', 'temp': t_min},
            {'time': '09:00', 'temp': round((t_min + t_cur) / 2)},
            {'time': '12:00', 'temp': t_cur - 1},
            {'time': '15:00', 'temp': t_cur},
            {'time': '18:00', 'temp': round((t_cur + t_max) / 2)},
            {'time': '21:00', 'temp': t_max},
        ]
        wave_path, wave_active_x, wave_active_y = build_wave_path(
            [h['temp'] for h in hourly_temps]
        )

    today      = datetime.date.today()
    day_labels = [
        {'name': (today + datetime.timedelta(days=i)).strftime('%A'), 'is_today': i == 0}
        for i in range(-2, 4)
    ]

    return render(request, 'weatherapp/index.html', {
        'city':           city,
        'data':           data,
        'error':          error,
        'city_video':     city_video,
        'hourly_temps':   hourly_temps,
        'day_labels':     day_labels,
        'wave_path':      wave_path,
        'wave_active_x':  wave_active_x,
        'wave_active_y':  wave_active_y,
        'aqi':            aqi_data,
        'forecast':       forecast,
        'insights':       insights,
        'alerts':         alerts,
        'activities':     activities,
        'b2b':            b2b,
        'social':         social,
        'travel':         travel,
        # new
        'impact':         impact,
        'mood':           mood,
        'creator':        creator,
        'lifestyle':      lifestyle,
        'event_date':     event_date,
        'event_forecast': event_forecast,
        'compare_input':  compare_input,
        'comparison':     comparison,
        'chart_labels':   json.dumps(chart_labels),
        'chart_max':      json.dumps(chart_max),
        'chart_min':      json.dumps(chart_min),
        'chart_hum':      json.dumps(chart_hum),
        'chart_wind':     json.dumps(chart_wind),
        'chart_pop':      json.dumps(chart_pop),
        'forecast_json':  json.dumps(forecast),
    })

# ══════════════════════════════════════════════════════════════════
#  AJAX: City Comparison (no page reload)
# ══════════════════════════════════════════════════════════════════

def compare_ajax(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    cities_raw = request.POST.get('cities', '')
    city_list  = [c.strip() for c in cities_raw.replace(';', ',').split(',') if c.strip()]
    if not city_list:
        return JsonResponse({'results': []})
    raw = get_comparison_data(city_list[:4])
    out = []
    for row in raw:
        if row['data']:
            d = row['data']
            out.append({
                'city':       row['city'],
                'temp':       d['main']['temp'],
                'feels_like': d['main']['feels_like'],
                'humidity':   d['main']['humidity'],
                'wind':       d['wind']['speed'],
                'pressure':   d['main']['pressure'],
                'uv_index':   d.get('uv_index', 0),
                'condition':  d['weather'][0]['main'],
                'icon':       d['weather'][0]['icon'],
                'error':      None,
            })
        else:
            out.append({'city': row['city'], 'error': row['error']})
    return JsonResponse({'results': out})


# ══════════════════════════════════════════════════════════════════
#  Groq AI Chat
# ══════════════════════════════════════════════════════════════════

def chat_ajax(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    message = request.POST.get('message', '').strip()
    if not message:
        return JsonResponse({'error': 'No message'}, status=400)

    city      = request.POST.get('city',      'your city')
    temp      = request.POST.get('temp',      'N/A')
    feels     = request.POST.get('feels',     'N/A')
    humidity  = request.POST.get('humidity',  'N/A')
    wind      = request.POST.get('wind',      'N/A')
    condition = request.POST.get('condition', 'N/A')
    desc      = request.POST.get('desc',      'N/A')
    uv        = request.POST.get('uv',        '0')
    vis       = request.POST.get('vis',       '10000')
    aqi       = request.POST.get('aqi',       'N/A')
    aqi_label = request.POST.get('aqiLabel',  'N/A')
    pop       = request.POST.get('pop',       '0')

    system_prompt = (
        f"You are WeatherBuddy, a warm, friendly, and knowledgeable weather companion for SkyPulse. "
        f"Your personality: caring, cheerful, and genuinely helpful - like a knowledgeable friend who happens to be a weather expert. "
        f"Speak naturally and conversationally. Use emojis occasionally to add warmth. "
        f"Keep replies concise (2-3 sentences max) but always give specific, actionable advice based on the actual data. "
        f"If conditions are dangerous, put safety first.\n\n"
        f"Live weather data for {city}:\n"
        f"Temperature: {temp}C (feels like {feels}C)\n"
        f"Condition: {condition} ({desc})\n"
        f"Humidity: {humidity}%\n"
        f"Wind speed: {wind} m/s\n"
        f"UV Index: {uv}\n"
        f"Visibility: {vis} m\n"
        f"Air Quality (AQI): {aqi}/5 ({aqi_label})\n"
        f"Rain probability: {pop}%"
    )

    try:
        resp = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {GROQ_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'model':    'llama-3.3-70b-versatile',
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user',   'content': message},
                ],
                'max_tokens':  150,
                'temperature': 0.75,
            },
            timeout=12,
        )
        data  = resp.json()
        reply = data['choices'][0]['message']['content'].strip()
        return JsonResponse({'reply': reply})
    except Exception:
        return JsonResponse({'reply': "I'm having a little trouble connecting right now. Give me a moment and try again!"})


# ══════════════════════════════════════════════════════════════════
#  City Autocomplete
# ══════════════════════════════════════════════════════════════════

def city_suggest(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    try:
        resp = requests.get(
            'https://api.openweathermap.org/geo/1.0/direct',
            params={'q': q, 'limit': 6, 'appid': OWM_KEY},
            timeout=4,
        )
        data = resp.json()
        seen, results = set(), []
        for item in data:
            name  = item.get('name', '')
            state = item.get('state', '')
            country = item.get('country', '')
            label = name
            if state:
                label += f', {state}'
            if country:
                label += f', {country}'
            if label not in seen:
                seen.add(label)
                results.append({'name': name, 'label': label})
        return JsonResponse({'results': results})
    except Exception:
        return JsonResponse({'results': []})
