from uuid import uuid1
from time import time
from random import choice
from urllib import urlopen 
from itertools import combinations
from math import pi, sin, cos, atan2, hypot
from os.path import basename, splitext 
from urlparse import urljoin, urlparse
from csv import DictReader
from json import dumps
import re 

from util import connect_domain, check_url

from boto.exception import SDBResponseError
from shapely.geometry import Polygon
from ModestMaps.Geo import MercatorProjection
from ModestMaps.Core import Point

required_fields = ['map_title', 'date', 'image_url']
reserved_keys = ['image','large','thumb','atlas','version'] #map

def generate_id():
    '''
    '''
    return str(uuid1())

def connect_domains(key, secret, prefix):
    '''
    '''
    suffixes = 'atlases', 'maps', 'rough_placements'
    domains = [connect_domain(key, secret, prefix+suffix) for suffix in suffixes]
    
    return domains
    
def validate_required_fields(keys):
    errors = []
    for field in required_fields: 
        if field not in keys:
            errors.append(field)
        
    return errors

def slugify(w):
    w = w.strip().lower()
    return re.sub(r'\W+','_',w)
        
def normalize_rows(rows):
    normalized = []
    for row in rows:
        obj = {}
        for item in row:
            norm_key = slugify(item)

            if norm_key in reserved_keys:
                norm_key = "__" + norm_key
                
            obj[norm_key] = row[item]
        normalized.append(obj) 
        
    return normalized
    
def create_atlas(domain, mysql, queue, url, name, affiliation):
    '''
    ''' 
    temp = None 
    if not check_url(url):
        return {'error':"There's no file at that URL: <a href='%s'>%s</a>. Please <a href='/upload'>try again</a>."%(url,url)} 
    
    try:
        temp = DictReader(urlopen(url))
    except IOError:
        return {'error':"There's no file at that URL: <a href='%s'>%s</a>. Please <a href='/upload'>try again</a>."%(url,url)}
    
    if not temp.fieldnames:
        return {'error':"We couldn't work out if that file is even a CSV. Can you grab the URL again, and try another <a href='/upload'>upload</a>?"}
    
    try:
        rows = normalize_rows(list(temp))
    except:
        return {'error':"We couldn't work out if that file is even a CSV. Can you grab the URL again, and try another <a href='/upload'>upload</a>?"}  
        
    
    
    
    # normalize keys
    #keys = [key.lower().replace(' ', '_') for key in rows[0].keys()]
    
    missing_fields = validate_required_fields(rows[0].keys()) 
    
    #
    # validate fields
    #
    if len(missing_fields) > 0:
        return {'error':"Your CSV doesn't have all the required columns. You must include map_title, date and image_url.<br/>Please <a href='/upload'>try again</a>."}
    
    #
    # validate images
    # 
    
    # need to know the image_col name
    #img_col = filter(lambda x: x.lower().replace(' ', '_') == "image_url", rows[0].keys())[0]
    invalids = []
    for idx, row in enumerate(rows):
        row_num = idx+1
        for req in required_fields:
            if not row[req]:
                invalids.append({'idx':row_num,'err':"Missing %s"%req})
        
        if row['image_url']:    
            valid_url = check_url(row['image_url'])
            if not valid_url:
                invalids.append({'idx':row_num,'err':"Couldn't find an image at <a href='%s'>%s</a>"%(row['image_url'],row['image_url'])})
            
    if len(invalids):
        return {'error':"Some of your map entries don't work, because...","rows":invalids} 
    

    #if 'address' not in row:
        #raise ValueError('Missing "address" in %(url)s' % locals())

    #
    # Add an entry for the atlas to the atlases table.
    #
    atlas = domain.new_item(str(uuid1()))
    atlas['href'] = url
    atlas['timestamp'] = time()
    atlas['title'] = name
    atlas['affiliation'] = affiliation
    atlas['map_count'] = len(rows)
    atlas['status'] = 'processing maps'
    atlas.save()
    
    #
    # add maps
    #
    for row in rows:
        scheme, host, path, q, p, f = urlparse(row['image_url'])

        image_name = basename(path)
        map_id = generate_id()
        map_img = 'maps/%s/%s' % (map.name, image_name)
        map_lrg = 'maps/%s/%s-large.jpg' % (map.name, splitext(image_name)[0])
        map_thb = 'maps/%s/%s-thumb.jpg' % (map.name, splitext(image_name)[0])
        map_atl = atlas.name
        map_sts = 'empty'
        map_ext = dumps(row)
        
        mysql.execute('''INSERT INTO maps
                         (id, atlas_id, image, large, thumb, status, extras_json)
                         VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                      (map_id, map_atl, map_img, map_lrg, map_thb, map_sts, map_ext))
        
        message = queue.new_message('create map %s' % map_id)
        queue.write(message)

    return {'success':atlas}

             
def choose_map(map_dom, atlas_id=None, skip_map_id=None):
    '''
    '''
    q = ['select * from `%s`' % (map_dom.name), 'limit 100']
    
    if atlas_id:
        q.insert(1, 'where atlas = "%s"' % atlas_id)
    
    maps = list(map_dom.select(' '.join(q)))
    map = choice(maps)
    
    while map.name == skip_map_id and len(maps) > 1:
        map = choice(maps)
        
    return map

def place_roughly(map_dom, mysql, queue, map, ul_lat, ul_lon, lr_lat, lr_lon):
    '''
    '''
    #
    # Generate a new placement and save it.
    #
    mysql.execute('''INSERT INTO placements
                     (map_id, timestamp, ul_lat, ul_lon, lr_lat, lr_lon)
                     VALUES (%s, %s, %s, %s, %s, %s)''',
                  (map.name, int(time()), ul_lat, ul_lon, lr_lat, lr_lon))
    
    #
    # Update the map item with current placement agreement.
    # Try a few times in case of race conditions.
    #
    for attempt in (1, 2, 3):
        try:
            update_map_rough_consensus(map_dom, mysql, map)

            message = queue.new_message('tile map %s' % map.name)
            queue.write(message)

            break

        except SDBResponseError, e:
            if attempt == 3:
                raise

def deg2rad(deg):
    return pi * float(deg) / 180

def rad2deg(rad):
    return 180 * float(rad) / pi

def average_thetas(thetas):
    ''' Average together a bunch of angles.
    '''
    xs, ys = map(cos, thetas), map(sin, thetas)
    x, y = sum(xs) / len(xs), sum(ys) / len(ys)
    return atan2(y, x)

def build_rough_placement_polygon(aspect, ul_lat, ul_lon, lr_lat, lr_lon):
    ''' Return rough placement geometry.
    
        Length of map hypotenuse in mercator units, angle of hypotenuse
        in radians counter-clockwise from due east, and footprint polygon.
    '''
    merc = MercatorProjection(0)
    
    #
    # Get the natural angle of the hypotenuse from map aspect ratio,
    # measured from the lower-right to the upper-left corner and expressed
    # in CCW radians from due east.
    #
    base_theta = atan2(1, -float(aspect))

    #
    # Convert corner lat, lons to conformal mercator projection
    #
    ul = merc.rawProject(Point(deg2rad(ul_lon), deg2rad(ul_lat)))
    lr = merc.rawProject(Point(deg2rad(lr_lon), deg2rad(lr_lat)))
    
    #
    # Derive dimensions of map in mercator units.
    #
    map_hypotenuse = hypot(ul.x - lr.x, ul.y - lr.y)
    map_width = map_hypotenuse * sin(base_theta - pi/2)
    map_height = map_hypotenuse * cos(base_theta - pi/2)
    
    #
    # Get the placed angle of the hypotenuse from the two placed corners,
    # again measured from the lower-right to the upper-left corner and
    # expressed in CCW radians from due east.
    #
    place_theta = atan2(ul.y - lr.y, ul.x - lr.x)
    diff_theta = place_theta - base_theta
    
    #
    # Derive the other two corners of the roughly-placed map,
    # and make a polygon in mercator units.
    #
    dx = map_height * sin(diff_theta)
    dy = map_height * cos(diff_theta)
    ur = Point(lr.x - dx, lr.y + dy)
    
    dx = map_width * cos(diff_theta)
    dy = map_width * sin(diff_theta)
    ll = Point(lr.x - dx, lr.y - dy)
    
    poly = Polygon([(ul.x, ul.y), (ur.x, ur.y), (lr.x, lr.y), (ll.x, ll.y), (ul.x, ul.y)])
    
    return map_hypotenuse, diff_theta, poly

def calculate_corners(aspect, x, y, size, theta):
    ''' Return latitude, longitude corners for a geometric placement.
    '''
    merc = MercatorProjection(0)
    
    #
    # Get the natural angle of the hypotenuse from map aspect ratio,
    # measured from the lower-right to the upper-left corner and expressed
    # in CCW radians from due east.
    #
    base_theta = atan2(1, -float(aspect))
    
    #
    # Derive center-to-corners offset from natural angle and placement theta.
    #
    place_theta = base_theta + theta
    
    dx = sin(place_theta - pi/2) * size/2
    dy = cos(place_theta - pi/2) * size/2

    ul = Point(x - dx, y + dy)
    lr = Point(x + dx, y - dy)
    
    #
    # Convert back to degree latitude and longitude
    #
    ul = merc.rawUnproject(ul)
    lr = merc.rawUnproject(lr)
    
    ul_lat, ul_lon = rad2deg(ul.y), rad2deg(ul.x)
    lr_lat, lr_lon = rad2deg(lr.y), rad2deg(lr.x)
    
    return ul_lat, ul_lon, lr_lat, lr_lon

def update_map_rough_consensus(map_dom, mysql, map):
    '''
    '''
    #
    # Get all other existing placements and fresh version of the map.
    #
    mysql.execute('''SELECT ul_lat, ul_lon, lr_lat, lr_lon
                     FROM placements
                     WHERE map_id = %s''', (map.name, ))
    
    placements = mysql.fetchall()
    
    map = map_dom.get_item(map.name, consistent_read=True)
    
    if len(placements) == 0:
        raise Exception("Got no placements - why?")
    
    #
    # Get geometries for each rough placement.
    #
    roughs = [build_rough_placement_polygon(map['aspect'], ul_lat, ul_lon, lr_lat, lr_lon)
              for (ul_lat, ul_lon, lr_lat, lr_lon) in placements]
    
    sizes, thetas, polygons = zip(*roughs)
    
    #
    # Using the area containing all pairwise-overlaps between polygons (i.e.
    # those places with two or more agreed votes), narrow the list of polygons
    # down to only those that cover 90%+ of this area.
    #
    pair_overlaps = [p1.intersection(p2) for (p1, p2) in combinations(polygons, 2)]
    union_pairs = reduce(lambda a, b: a.union(b), pair_overlaps)
    
    good_indexes = [index for (index, polygon) in enumerate(polygons)
                    if (polygon.area / union_pairs.area) > 0.9]
    
    good_indexes = good_indexes[-3:]
    
    good_sizes = [sizes[i] for i in good_indexes]
    good_thetas = [thetas[i] for i in good_indexes]
    good_centers = [polygons[i].centroid for i in good_indexes]
    
    #
    # Determine average geometries for the good placements.
    #
    avg_size = sum(good_sizes) / len(good_sizes)
    avg_theta = average_thetas(good_thetas)
    avg_x = sum([c.x for c in good_centers]) / len(good_centers)
    avg_y = sum([c.y for c in good_centers]) / len(good_centers)
    
    #
    # Combine the placements to come up with consensus.
    #
    ul_lat, ul_lon, lr_lat, lr_lon = calculate_corners(map['aspect'], avg_x, avg_y, avg_size, avg_theta)

    consensus = dict(ul_lat='%.8f' % ul_lat, ul_lon='%.8f' % ul_lon,
                     lr_lat='%.8f' % lr_lat, lr_lon='%.8f' % lr_lon,
                     status='rough-placed')
    
    #
    # Update the map with new information
    #
    
    if 'version' not in map:
        consensus['version'] = 1
        map_dom.put_attributes(map.name, consensus, expected_value=['version', False])
    
    else:
        consensus['version'] = 1 + int(map['version'])
        map_dom.put_attributes(map.name, consensus, expected_value=['version', map['version']])
