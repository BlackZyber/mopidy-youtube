# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import re
import string
import unicodedata
from multiprocessing.pool import ThreadPool
from urlparse import parse_qs, urlparse

from mopidy import backend
from mopidy.models import Album, SearchResult, Track

import pafy

import pykka

import requests

from mopidy_youtube import logger

yt_api_endpoint = 'https://www.googleapis.com/youtube/v3/'
yt_key = 'AIzaSyAl1Xq9DwdE_KD4AtPaE4EJl3WZe2zCqg4'
session = requests.Session()

video_uri_prefix = 'youtube:video'
search_uri = 'youtube:search'


def resolve_track(track, stream=False):
    logger.debug("Resolving YouTube for track '%s'", track)
    if hasattr(track, 'uri'):
        return resolve_url(track.comment, stream)
    else:
        return resolve_url(track.split('.')[-1], stream)


def safe_url(uri):
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    safe_uri = unicodedata.normalize(
        'NFKD',
        unicode(uri)
    ).encode('ASCII', 'ignore')
    return re.sub(
        '\s+',
        ' ',
        ''.join(c for c in safe_uri if c in valid_chars)
    ).strip()


def resolve_url(url, stream=False):
    try:
        video = pafy.new(url)
        if not stream:
            uri = '%s/%s.%s' % (
                video_uri_prefix, safe_url(video.title), video.videoid)
        else:
            uri = video.getbestaudio()
            if not uri:  # get video url
                uri = video.getbest()
            logger.debug('%s - %s %s %s' % (
                video.title, uri.bitrate, uri.mediatype, uri.extension))
            uri = uri.url
        if not uri:
            return
    except Exception as e:
        # Video is private or doesn't exist
        logger.info(e.message)
        return

    images = []
    if video.bigthumb is not None:
        images.append(video.bigthumb)
    if video.bigthumbhd is not None:
        images.append(video.bigthumbhd)

    track = Track(
        name=video.title,
        comment=video.videoid,
        length=video.length * 1000,
        album=Album(
            name='YouTube',
            images=images
        ),
        uri=uri
    )
    return track


def search_youtube(q):
    query = {
        'part': 'id',
        'maxResults': 15,
        'type': 'video',
        'q': q,
        'key': yt_key
    }
    result = session.get(yt_api_endpoint+'search', params=query)
    data = result.json()

    resolve_pool = ThreadPool(processes=16)
    playlist = [item['id']['videoId'] for item in data['items']]

    playlist = resolve_pool.map(resolve_url, playlist)
    resolve_pool.close()
    return [item for item in playlist if item]


def resolve_playlist(url):
    resolve_pool = ThreadPool(processes=16)
    logger.info("Resolving YouTube-Playlist '%s'", url)
    playlist = []

    page = 'first'
    while page:
        params = {
            'playlistId': url,
            'maxResults': 50,
            'key': yt_key,
            'part': 'contentDetails'
        }
        if page and page != "first":
            logger.debug("Get YouTube-Playlist '%s' page %s", url, page)
            params['pageToken'] = page

        result = session.get(yt_api_endpoint+'playlistItems', params=params)
        data = result.json()
        page = data.get('nextPageToken')

        for item in data["items"]:
            video_id = item['contentDetails']['videoId']
            playlist.append(video_id)

    playlist = resolve_pool.map(resolve_url, playlist)
    resolve_pool.close()
    return [item for item in playlist if item]


class YouTubeBackend(pykka.ThreadingActor, backend.Backend):
    def __init__(self, config, audio):
        super(YouTubeBackend, self).__init__()
        self.config = config
        self.library = YouTubeLibraryProvider(backend=self)
        self.playback = YouTubePlaybackProvider(audio=audio, backend=self)

        self.uri_schemes = ['youtube', 'yt']


class YouTubeLibraryProvider(backend.LibraryProvider):
    def lookup(self, track):
        if 'yt:' in track:
            track = track.replace('yt:', '')

# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import re
import string
import unicodedata
from urlparse import parse_qs, urlparse

from mopidy import backend
from mopidy.models import Album, Artist, SearchResult, Track

import pykka

from mopidy_youtube import logger, youtube

# A typical interaction:
# 1. User searches for a keyword (YouTubeLibraryProvider.search)
# 2. User adds a track to the queue (YouTubeLibraryProvider.lookup)
# 3. User plays a track from the queue (YouTubePlaybackProvider.translate_uri)
#
# step 1 requires only 2 API calls. Data for the next steps are loaded in the
# background, so steps 2/3 are usually instantaneous.


# youtube:video/<title>.<id> ==> <id>
def extract_id(uri):
    return uri.split('.')[-1]


def safe_url(uri):
    valid_chars = '-_.() %s%s' % (string.ascii_letters, string.digits)
    safe_uri = unicodedata.normalize(
        'NFKD',
        unicode(uri)
    ).encode('ASCII', 'ignore')
    return re.sub(
        '\s+',
        ' ',
        ''.join(c for c in safe_uri if c in valid_chars)
    ).strip()


class YouTubeBackend(pykka.ThreadingActor, backend.Backend):
    def __init__(self, config, audio):
        super(YouTubeBackend, self).__init__()
        self.config = config
        self.library = YouTubeLibraryProvider(backend=self)
        self.playback = YouTubePlaybackProvider(audio=audio, backend=self)

        ytconf = config['youtube']
        youtube.API.key = ytconf['api_key']
        youtube.API.search_results = ytconf['search_results']
        youtube.Playlist.max_videos = ytconf['playlist_max_videos']

        self.uri_schemes = ['youtube', 'yt']


class YouTubeLibraryProvider(backend.LibraryProvider):

    # Called when browsing or searching the library. To avoid horrible browsing
    # performance, and since only search makes sense for youtube anyway, we we
    # only answer queries for the 'any' field (for instance a {'artist': 'U2'}
    # query is ignored).
    #
    # For performance we only do 2 API calls before we reply, one for search
    # (youtube.Entry.search) and one to fetch video_count of all playlists
    # (youtube.Playlist.load_info).
    #
    # We also start loading 2 things in the background:
    #  - info for all videos
    #  - video list for all playlists
    # Hence, adding search results to the playing queue (see
    # YouTubeLibraryProvider.lookup) will most likely be instantaneous, since
    # all info will be ready by that time.
    #
    def search(self, query=None, uris=None, exact=False):
        # TODO Support exact search
        logger.info('youtube LibraryProvider.search "%s"', query)

        # handle only searching (queries with 'any') not browsing!
        if not (query and 'any' in query):
            return None

        search_query = ' '.join(query['any'])
        logger.info('Searching YouTube for query "%s"', search_query)

        try:
            entries = youtube.Entry.search(search_query)
        except Exception:
            return None

        # load playlist info (to get video_count) of all playlists together
        playlists = [e for e in entries if not e.is_video]
        youtube.Playlist.load_info(playlists)

        tracks = []
        for entry in entries:
            if entry.is_video:
                uri_base = 'youtube:video'
                album = 'YouTube Video'
            else:
                uri_base = 'youtube:playlist'
                album = 'YouTube Playlist (%s videos)' % \
                        entry.video_count.get()

            tracks.append(Track(
                name=entry.title.get(),
                comment=entry.id,
                length=0,
                artists=[Artist(name=entry.channel.get())],
                album=Album(
                    name=album,
                    images=entry.thumbnails.get(),
                ),
                uri='%s/%s.%s' %
                    (uri_base, safe_url(entry.title.get()), entry.id)
            ))

        # load video info and playlist videos in the background. they should be
        # ready by the time the user adds search results to the playing queue
        videos = [e for e in entries if e.is_video]
        youtube.Video.load_info(videos)

        for pl in playlists:
            pl.videos  # start loading

        return SearchResult(
            uri='youtube:search',
            tracks=tracks
        )

    # Called when the user adds a track to the playing queue, either from the
    # search results, or directly by adding a yt:http://youtube.com/.... uri.
    # uri can be of the form
    #   [yt|youtube]:<url to youtube video>
    #   [yt|youtube]:<url to youtube playlist>
    #   youtube:video/<title>.<id>
    #   youtube:playlist/<title>.<id>
    #
    # If uri is a video then a single track is returned. If it's a playlist the
    # list of all videos in the playlist is returned.
    #
    # We also start loading the audio_url of all videos in the background, to
    # be ready for playback (see YouTubePlaybackProvider.translate_uri).
    #
    def lookup(self, uri):
        logger.info('youtube LibraryProvider.lookup "%s"', uri)

        video_id = playlist_id = None

        if 'youtube.com' in uri:
            url = urlparse(uri.replace('yt:', '').replace('youtube:', ''))
            req = parse_qs(url.query)
            if 'list' in req:
                playlist_id = req.get('list')[0]
            else:
                video_id = req.get('v')[0]

        elif 'video/' in uri:
            video_id = extract_id(uri)
        else:
            playlist_id = extract_id(uri)

        if video_id:
            video = youtube.Video.get(video_id)
            video.audio_url  # start loading

            return [Track(
                name=video.title.get(),
                comment=video.id,
                length=video.length.get() * 1000,
                artists=[Artist(name=video.channel.get())],
                album=Album(
                    name='YouTube Video',
                    images=video.thumbnails.get(),
                ),
                uri='youtube:video/%s.%s' %
                    (safe_url(video.title.get()), video.id)
            )]
        else:
            playlist = youtube.Playlist.get(playlist_id)
            if not playlist.videos.get():
                logger.info('cannot load playlist "%s"', uri)
                return []

            # ignore videos for which no info was found (removed, etc)
            videos = [v for v in playlist.videos.get()
                      if v.length.get() is not None]

            # load audio_url in the background to be ready for playback
            for video in videos:
                video.audio_url  # start loading

            return [Track(
                name=video.title.get(),
                comment=video.id,
                length=video.length.get() * 1000,
                track_no=count,
                artists=[Artist(name=video.channel.get())],
                album=Album(
                    name=playlist.title.get(),
                    images=playlist.thumbnails.get(),
                ),
                uri='youtube:video/%s.%s' %
                    (safe_url(video.title.get()), video.id)
            ) for count, video in enumerate(videos, 1)]


class YouTubePlaybackProvider(backend.PlaybackProvider):

    # Called when a track us ready to play, we need to return the actual url of
    # the audio. uri must be of the form youtube:video/<title>.<id>
    # (only videos can be played, playlists are expended into tracks by
    # YouTubeLibraryProvider.lookup)
    #
    def translate_uri(self, uri):
        logger.info('youtube PlaybackProvider.translate_uri "%s"', uri)

        if 'youtube:video/' not in uri:
            return None

        try:
            id = extract_id(uri)
            return youtube.Video.get(id).audio_url.get()
        except Exception as e:
            logger.error('translate_uri error "%s"', e)
            return None
