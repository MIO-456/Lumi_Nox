"""Validate model pause controls before any public text or audio is submitted.

Only canonical pause controls are accepted; suspicious markup fails closed.
This deliberately does not promise to detect arbitrary natural-language narration.
"""
import html
import re
import unicodedata
import xml.etree.ElementTree as ET


from spoken_output import clean_spoken_text

TAG = '<break time="400ms"/>'
MARKER = '\ue000'




def supports_pause_tags(model):
    # This is the provider/model family tested with the project's cloned voices.
    return str(model or '').startswith('qwen-audio-3.1-')


def render_ssml(plain, offsets):
    """Only this function creates executable markup; callers carry plain text."""
    if not offsets:
        return plain
    if (list(offsets) != sorted(set(offsets))
            or any(type(n) is not int or not 0 < n <= len(plain) for n in offsets)):
        raise ValueError('Invalid pause offsets')
    parts = []; cursor = 0
    for offset in offsets:
        parts.extend((html.escape(plain[cursor:offset], quote=False), TAG))
        cursor = offset
    parts.append(html.escape(plain[cursor:], quote=False))
    ssml = '<speak>' + ''.join(parts) + '</speak>'
    root = ET.fromstring(ssml)
    if ''.join(root.itertext()) != plain:
        raise ValueError('SSML changed the spoken text')
    ssml.encode('utf-8', errors='strict')
    return ssml


def speech_segments(plain, offsets):
    """Split validated text, retaining pauses at the end of a spoken sentence."""
    for match in re.finditer(r'[^。！？!?~]+[。！？!?~]*[\"”’」』]*|[。！？!?~]+', plain):
        raw = match.group()
        left = match.start() + len(raw) - len(raw.lstrip())
        right = match.end() - (len(raw) - len(raw.rstrip()))
        text = plain[left:right]
        if text:
            positions = sorted(set(min(n, right) - left for n in offsets
                                   if left < n <= match.end()))
            yield text, positions


def blocked(reason, raw_tags=0):
    return dict(ok=False, reason=reason, plain='', ssml='', pause_offsets=[],
                raw_tags=raw_tags, dropped_positions=0)


def suspicious_control(text):
    # Inspection only: do not silently repair and then execute a misspelled tag.
    check=unicodedata.normalize('NFKC',text)
    check=''.join(ch for ch in check if unicodedata.category(ch)!='Cf')
    for _ in range(6):
        decoded=html.unescape(check)
        if decoded==check: break
        check=decoded
    # Preserve ordinary numerical comparisons; everything else with angle marks
    # is ambiguous in this explicitly markup-bearing output channel.
    check=re.sub(r'(?<=\d)\s*[<>]\s*(?=\d)', '', check)
    if any(ch in check for ch in '<>〈〉⟨⟩≺≻≪≫'):
        return 'unknown_or_malformed_markup'
    if re.search(r'&(?:amp;)*(?:lt|gt|#0*6[02]|#x0*3[ce])(?:;|\b)',check,re.I):
        return 'encoded_markup'
    if re.search(r'(?<![A-Za-z])(?:time|strength)\s*[:=]',check,re.I):
        return 'orphan_control_attribute'
    if re.search(r'(?<![A-Za-z])(?:break|pause|speak|prosody)\s*(?:/|[:=]|\d)',check,re.I):
        return 'orphan_control_name'
    if re.search(r'(?<![A-Za-z])(?:break|pause|speak|prosody)\s+(?:time|strength|duration)(?![A-Za-z])',check,re.I):
        return 'orphan_control_phrase'
    if re.search(r'(?:停顿|暂停|pause)\s*:?\s*(?:约)?\s*\d+(?:\.\d+)?\s*(?:毫秒|ms|秒)',check,re.I):
        return 'verbal_pause_instruction'
    return None


def prepare_output(raw, speaker, *, finish_reason='stop', cancelled=False, max_chars=None):
    if cancelled:
        return blocked('cancelled')
    if finish_reason!='stop':
        return blocked('incomplete_generation')
    if MARKER in raw:
        return blocked('reserved_internal_marker')
    raw_tags=raw.count(TAG)
    without=raw.replace(TAG,'')
    risk=suspicious_control(without)
    if risk:
        return blocked(risk,raw_tags)

    # The same production cleaner sees the entire text, so bracket/quote/role
    # state spans pause positions. Markers inside removed stage directions vanish.
    cleaned=clean_spoken_text(raw.replace(TAG,MARKER),speaker=speaker)
    # The legacy cleaner recovers quoted speech inside brackets. If that recovery
    # exposes a pause annotation next to our control, it is not safe spoken text.
    annotation=r'(?:停顿|暂停|(?<![A-Za-z])(?:pause|break)(?![A-Za-z]))'
    if (re.search(annotation+r'\s*[:：]?\s*'+MARKER,cleaned,re.I)
            or re.search(MARKER+r'\s*'+annotation,cleaned,re.I)):
        return blocked('recovered_pause_annotation',raw_tags)
    chars=[]; offsets=[]
    for ch in cleaned:
        if ch==MARKER:
            offsets.append(len(chars))
        elif ch.isspace():
            if chars and chars[-1]!=' ': chars.append(' ')
        else:
            chars.append(ch)
    plain=''.join(chars).rstrip()
    risk=suspicious_control(plain)
    if risk:
        return blocked('post_clean_'+risk,raw_tags)
    char_limit_reached = max_chars is not None and len(plain) > max_chars
    if char_limit_reached:
        # Keep only complete sentences. The cap counts spoken characters, never XML.
        ends = [m.end() for m in re.finditer(r'[。！？!?~]+[\"”’」』]*', plain)
                if m.end() <= max_chars]
        if not ends:
            return blocked('char_limit_without_complete_sentence', raw_tags)
        plain = plain[:ends[-1]].rstrip()
    keep=[]
    for offset in offsets:
        if offset >= len(plain):
            continue
        # A model may put a tag after a newline. Attribute that pause to the
        # preceding words, so stripping sentence-edge whitespace cannot lose it.
        offset = len(plain[:offset].rstrip())
        left=plain[:offset].rstrip()
        right=plain[offset:].lstrip()
        # Drop only empty edge/repeated pauses, retaining every spoken word.
        # Punctuation is not a reliable proxy for a semantic pause boundary.
        if (not any(c.isalnum() for c in left) or not any(c.isalnum() for c in right)
                or (keep and not any(c.isalnum() for c in plain[keep[-1]:offset]))):
            continue
        keep.append(offset)
    try:
        ssml = render_ssml(plain, keep)
    except (ET.ParseError, ValueError, UnicodeError):
        return blocked('invalid_rendered_xml',raw_tags)
    return dict(ok=True,reason='ok' if plain else 'empty',plain=plain,ssml=ssml,
                pause_offsets=keep,raw_tags=raw_tags,dropped_positions=raw_tags-len(keep),
                char_limit_reached=char_limit_reached)


class BufferedPauseOutput:
    """No fragment can escape before validation of the entire generated turn."""
    def __init__(self,speaker):
        self.speaker=speaker
        self._chunks=[]
        self._finished=False

    def feed(self,chunk):
        if self._finished: raise RuntimeError('already finalized')
        self._chunks.append(chunk)
        return ''  # No public output before final validation.

    def finish(self,*,finish_reason='stop',cancelled=False):
        if self._finished: raise RuntimeError('already finalized')
        self._finished=True
        return prepare_output(''.join(self._chunks),self.speaker,
                              finish_reason=finish_reason,cancelled=cancelled)
