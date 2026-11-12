"""One stream-safe text filter for spoken model output, events and history.

This removes explicit formatting, not unmarked natural-language narration.
It never calls a model, rewrites a sentence, or invents a fallback utterance.
"""
import re
import unicodedata
from collections import Counter

_BRACKETS = {'(': ')', '（': '）', '[': ']', '【': '】', '{': '}'}
_QUOTES = {'"': '"', '“': '”', '‘': '’', '「': '」', '『': '』'}
_ROLE = re.compile(r'(Lumi|Nox|Mio|观众)\s*(?:[·•]\s*)?(?:说|发言|弹幕)?\s*[:：]?', re.I)
_CONTROLS = re.compile(r'\\+[nrt]')


def _quote_end(text, start=0):
    end = _QUOTES[text[start]]
    index = start + 1
    while index < len(text):
        if text[index] == '\\':
            index += 2
        elif text[index] == end:
            return index
        else:
            index += 1
    return None


def _bracket_end(text):
    stack = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == '\\':
            index += 2
            continue
        if ch in _QUOTES:
            end = _quote_end(text, index)
            if end is None:
                return None
            index = end + 1
            continue
        if ch in _BRACKETS:
            stack.append(_BRACKETS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return index
        index += 1
    return None


class SpokenTextFilter:
    def __init__(self, speaker=None):
        self.speaker = (speaker or '').lower()
        self.pending = ''
        self.stopped = False
        self.reasons = Counter()
        self._last = ''
        self._quote = None

    @staticmethod
    def normalize(text):
        text = re.sub(r'\s+', ' ', text).strip()
        # Orphan quotes, brackets or punctuation must never create a TTS request.
        return text if any(ch.isalnum() for ch in text) else ''

    def feed(self, text):
        if self.stopped:
            return ''
        self.pending += text
        return self._drain(final=False)

    def finish(self):
        return self._drain(final=True)

    @property
    def in_quote(self):
        return self._quote is not None

    def _drain(self, final):
        output = []
        while self.pending and not self.stopped:
            text = self.pending
            ch = text[0]
            take, value = 1, ch
            # Keep tool-like text atomic until its JSON body is identified.
            # Otherwise removing {arguments} first would leave "shoot" to read.
            if not (self._last.isascii() and self._last.isalnum()):
                lower = text.lower()
                if not final and any(name.startswith(lower) or re.fullmatch(name + r'\s*', lower)
                                     for name in ('shoot', 'use_item')):
                    break
                tool = re.match(r'(?:shoot|use_item)\s*(?=\{)', text, re.I)
                if tool:
                    end = _bracket_end(text[tool.end():])
                    if end is None and not final:
                        break
                    self.pending = '' if end is None else text[tool.end()+end+1:]
                    self.reasons['tool_text'] += 1
                    continue
            if self._quote and ch == self._quote:
                self._quote = None
            elif ch in _BRACKETS:
                end = _bracket_end(text)
                if end is None:
                    if not final:
                        break
                    self.reasons['unclosed_bracket'] += 1
                    self.pending = ''
                    break
                take = end + 1
                body = text[1:end].strip()
                role = _ROLE.fullmatch(body)
                value = ''
                if role:
                    if self._quote:
                        value = text[:take]
                    elif self.speaker and role.group(1).lower() != self.speaker:
                        self.reasons['other_speaker'] += 1
                        self.stopped = True
                        self.pending = ''
                        break
                    else:
                        self.reasons['speaker_label'] += 1
                else:
                    # Recover explicit speech in ["speech" stage directions].
                    body = body.replace('\\"', '"')
                    if ch != '{' and body and body[0] in _QUOTES:
                        quote_end = _quote_end(body)
                        if quote_end is not None:
                            value = clean_spoken_text(body[1:quote_end])
                            self.reasons['quoted_speech_unwrapped'] += 1
                    if not value:
                        self.reasons['stage_direction'] += 1
            elif ch == '\\':
                match = re.match(r'\\+([nrt])', text)
                if match:
                    take, value = match.end(), ' '
                    self.reasons['escaped_whitespace'] += 1
                elif text.strip('\\') == '' and not final:
                    break
                else:
                    # Drop transport escaping, retaining the quoted content itself.
                    value = ''
                    self.reasons['escape_marker'] += 1
            elif ch == '/' and (not self._last or not self._last.isalnum()) and self._last not in '/:':
                if len(text) < 3 and not final and '/n'.startswith(text):
                    break
                if text.startswith('/n') and (len(text) == 2 or not text[2].isascii() or not text[2].isalnum()):
                    take, value = 2, ' '
                    self.reasons['slash_newline'] += 1
            elif ch == '`':
                if len(text) < 3 and not final and set(text) == {'`'}:
                    break
                fence = '```' if text.startswith('```') else '`'
                end = text.find(fence, len(fence))
                if end < 0:
                    if not final:
                        break
                    take, value = len(text), ''
                else:
                    take = end + len(fence)
                    value = '' if fence == '```' else clean_spoken_text(text[1:end])
                self.reasons['code_marker'] += 1
            elif ch == '<':
                if len(text) < 2 and not final:
                    break
                if re.match(r'</?[A-Za-z]', text) or text == '</':
                    end = text.find('>')
                    if end < 0:
                        if not final:
                            break
                        take, value = len(text), ''
                    elif re.match(r'<think[^>]*>', text, re.I):
                        closing = re.search(r'</think[^>]*>', text[end+1:], re.I)
                        if closing is None:
                            if not final:
                                break
                            take, value = len(text), ''
                        else:
                            take, value = end + 1 + closing.end(), ''
                    else:
                        take, value = end + 1, ''
                    self.reasons['control_tag'] += 1
            elif ch in _QUOTES and not self._quote:
                if len(text) == 1:
                    if not final:
                        break
                    value = ''
                    self.reasons['orphan_quote'] += 1
                else:
                    self._quote = _QUOTES[ch]
            elif ch in {'”', '」', '』'} and not self._quote:
                value = ''
                self.reasons['orphan_quote'] += 1
            elif ch in _BRACKETS.values():
                value = ''
                self.reasons['orphan_bracket'] += 1
            elif ch.isspace():
                value = ' '
            elif unicodedata.category(ch) in {'Cc', 'Cf'}:
                value = ''
            self.pending = text[take:]
            if value:
                output.append(value)
                self._last = value[-1]
        return ''.join(output)


def clean_spoken_text(text, speaker=None):
    cleaner = SpokenTextFilter(speaker)
    return cleaner.normalize(cleaner.feed(text or '') + cleaner.finish())
