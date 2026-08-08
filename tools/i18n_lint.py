#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AILEX — линтер надписей интерфейса (спека 4.46).

Зачем. Дефект «надпись осталась на языке исходника при не-русском интерфейсе»
виден в ИСХОДНИКЕ, а не в поведении: чтобы его найти, не нужно проходить урок на
телефоне — достаточно сверить каждую русскую строку, попадающую в DOM, с набором
переводимых. Поиск по скриншотам стоил по билду на находку и показывал только те
экраны, куда человек успел зайти; скрипт выдаёт весь список за секунду.

Три разные болезни — у каждой своё лечение:
  1. НЕТ В НАБОРЕ    — строка уходит в DOM, но её ключа нет ни в UI_EXTRA, ни в
                       английской таблице, и она не обёрнута в t(). Никакая
                       загрузка её не возьмёт. Лечение: обернуть в t() или внести
                       в UI_EXTRA.
  2. СКЛЕЙКА         — надпись стоит в одном текстовом узле с подстановкой
                       («Результат: ${n}/${m} верных»): ключ не совпадёт никогда.
                       Лечение: развести тегом — <span>Результат:</span> ${…}.
  3. ЗНАЧОК В ХВОСТЕ — «Следующий урок →». Приём 4.41 снимает только ВЕДУЩИЙ
                       значок; хвостовой оставляет строку неузнанной.
  4. АТРИБУТ         — placeholder / title / aria-label вне набора.
  5. БЕЗ t() ПРИ ВСТАВКЕ — строка кладётся в DOM уже после прохода локализации (присваивание
                       textContent/innerHTML, toast, Screen.loader). Наличие ключа в наборе тут
                       не спасает: переводить надо в момент вставки, обёрткой t().
  6. t() ВНЕ НАБОРА  — строка обёрнута в t() и потому переведётся, но её нет в UI_EXTRA,
                       а предзагрузка при создании профиля идёт именно по набору: человек
                       увидит её на языке исходника, пока не сработает фоновая догрузка.

Отключение проверки — только явной парой маркеров в коде:
    /* i18n-lint:off — причина */   …   /* i18n-lint:on */
Так помечены блоки, которые переводятся целиком через localizeBlocks (подсказки,
«Как пользоваться»): перевод по ключу к ним не применяется вовсе.

Запуск:  python3 tools/i18n_lint.py index.html
Код возврата: 0 — чисто, 1 — есть находки.
"""

import re
import sys

CYR = re.compile(r'[А-Яа-яЁё]')
LEAD = re.compile(r'^[^\w\u0400-\u04FF]+', re.UNICODE)
TAIL = re.compile(r'[^\w\u0400-\u04FF]+$', re.UNICODE)
PH = '\x00'                                   # место подстановки ${…}
ATTRS = ('placeholder', 'title', 'aria-label')

# перед этим знаком «/» начинает регулярный литерал, а не деление
RE_START = re.compile(r'(^|[=(,:;!&|?{}\[\]+\-*%~^<>]|\breturn|\btypeof|\bcase)\s*$')
# кавычка после этого — сравнение или ключ данных, а не надпись
CMP_TAIL = re.compile(r'(===|!==|==|!=|\.includes\(|\.startsWith\(|\.endsWith\('
                      r'|\.indexOf\(|\.split\(|\bcase\s|\[)\s*$')
# строка, приклеенная к выражению через +: на экране это ОДИН текстовый узел, поэтому ключ
# не совпадёт, даже если каждый кусок по отдельности лежит в наборе
CONCAT   = re.compile(r"\+\s*$")
CONCAT_R = re.compile(r"^\s*\+")
# контексты, где строка — это промпт к модели, а не надпись на экране
PROMPT_CTX = re.compile(r"(rules\s*:|schemaHint|task\s*:|Gemini\.call|hint\s*:\s*$)")
# вне разметки строка попадает на экран только через эти пути — остальное служебные значения,
# описания режимов для промптов, названия языков и прочее, что переводить не нужно
# сырая вставка: строка попадает в узел уже после прохода локализации, нужна обёртка t()
RAW_CTX = re.compile(r"(\.textContent\s*=|\.innerText\s*=|\.innerHTML\s*=|\.placeholder\s*=|"
                     r"(?<![-\w])\w*(?:[Ll]abel|[Cc]aption|[Bb]tnText)\w*\s*=)\s*[^=]{0,80}$")
# эти пути переводят сами (toast — с 4.48, Screen.loader и Screen.show — раньше):
# обёртка не нужна, но ключ обязан быть в наборе
SELF_CTX = re.compile(r"(\btoast\(|Screen\.loader\(|Screen\.show\()\s*[^=]{0,80}$")
DOM_CTX = re.compile(RAW_CTX.pattern + "|" + SELF_CTX.pattern)
# строка внутри вызова t(…) / lbl(…) переводится сама — реестр пополняется в рантайме
IN_T = re.compile(r"\b(I18N\.)?(t|lbl)\(\s*[^)]*$")
# те же вызовы, но взятые из исходника целиком — для проверки № 5
CALL = re.compile(r"\b(?:I18N\.)?(?:t|lbl)\(\s*'((?:[^'\\]|\\.)*)'")


class Chunk:
    def __init__(self, kind, text, line, ctx=''):
        self.kind, self.text, self.line, self.ctx = kind, text, line, ctx
        self.tail = ''
        self.inexpr = True      # внутри ${…}, то есть заведомо в разметке


def off_lines(src):
    """Номера строк, накрытых парой маркеров i18n-lint:off / i18n-lint:on."""
    lines = src.split('\n')
    off, cur = set(), None
    for i, ln in enumerate(lines, 1):
        if 'i18n-lint:off' in ln:
            cur = i
        elif 'i18n-lint:on' in ln and cur is not None:
            off.update(range(cur, i + 1))
            cur = None
    if cur is not None:
        off.update(range(cur, len(lines) + 1))
    return off


def scan(src):
    """Однопроходный сканер JS: собирает шаблонные литералы (с пометкой мест
    подстановки) и строковые литералы внутри подстановок. Комментарии,
    регулярные литералы и обычный код пропускает."""
    chunks, stack = [], []
    i, n, line = 0, len(src), 1
    prev = ''                                  # хвост последнего значащего кода

    def top():
        return stack[-1][0] if stack else None

    while i < n:
        c = src[i]

        if top() in (None, 'expr'):
            if src.startswith('//', i):
                j = src.find('\n', i)
                i = n if j < 0 else j
                continue
            if src.startswith('/*', i):
                j = src.find('*/', i + 2)
                seg = src[i:(j + 2) if j > 0 else n]
                line += seg.count('\n')
                i = (j + 2) if j > 0 else n
                continue
            if c == '/' and RE_START.search(prev[-16:]):
                j, cls, ok = i + 1, False, False
                while j < n:
                    ch = src[j]
                    if ch == '\\':
                        j += 2; continue
                    if ch == '[':
                        cls = True
                    elif ch == ']':
                        cls = False
                    elif ch == '/' and not cls:
                        ok = True; break
                    elif ch == '\n':
                        break
                    j += 1
                if ok:
                    i = j + 1
                    while i < n and src[i].isalpha():
                        i += 1
                    prev = '/re/'
                    continue
            if c in '\'"':
                q, j, buf = c, i + 1, []
                while j < n and src[j] != q:
                    if src[j] == '\\':
                        buf.append(src[j:j + 2]); j += 2; continue
                    if src[j] == '\n':
                        break
                    buf.append(src[j]); j += 1
                text = ''.join(buf)
                if CYR.search(text):
                    ch = Chunk('str', text, line, src[max(0, i - 60):i])
                    ch.tail = src[j + 1:j + 40]
                    ch.inexpr = (top() == 'expr')
                    chunks.append(ch)
                i = j + 1
                prev = "''"
                continue

        if c == '`':
            if top() == 'tpl':
                st = stack.pop()
                ch = Chunk('tpl', ''.join(st[1]), st[2], st[3])
                ch.inexpr = st[4]
                chunks.append(ch)
            else:
                stack.append(['tpl', [], line, src[max(0, i - 60):i], top() == 'expr'])
            i += 1
            prev = '`'
            continue

        if top() == 'tpl':
            if src.startswith('${', i):
                stack[-1][1].append(PH)
                stack.append(['expr', 0])
                i += 2
                prev = '('
                continue
            if c == '\\':
                stack[-1][1].append(src[i:i + 2]); i += 2; continue
            if c == '\n':
                line += 1
            stack[-1][1].append(c)
            i += 1
            continue

        if top() == 'expr':
            if c == '{':
                stack[-1][1] += 1
            elif c == '}':
                if stack[-1][1] == 0:
                    stack.pop()
                else:
                    stack[-1][1] -= 1

        if c == '\n':
            line += 1
            prev = ''
        elif not c.isspace():
            prev = (prev + c)[-24:]
        i += 1
    return chunks


def split_html(tpl):
    """Разбирает HTML-подобный текст: (текстовые узлы, значения атрибутов).
    Кавычки внутри тега разбор не путают."""
    nodes, attrs, buf = [], [], []
    i, n = 0, len(tpl)
    while i < n:
        if tpl[i] == '<':
            if buf:
                nodes.append(''.join(buf)); buf = []
            j, q = i + 1, None
            while j < n:
                ch = tpl[j]
                if q:
                    if ch == q:
                        q = None
                elif ch in '\'"':
                    q = ch
                elif ch == '>':
                    break
                j += 1
            tag = tpl[i:j]
            for a in ATTRS:
                attrs += re.findall(a + r'\s*=\s*"([^"]*)"', tag)
                attrs += re.findall(a + r"\s*=\s*'([^']*)'", tag)
            i = j + 1
            continue
        buf.append(tpl[i])
        i += 1
    if buf:
        nodes.append(''.join(buf))
    return nodes, attrs


def known_set(src):
    keys = set()
    m = re.search(r'const UI_EXTRA = \[(.*?)\n\];', src, re.S)
    if m:
        keys |= set(re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1)))
    m = re.search(r'\ben:\s*\{(.*?)\n\s*\}\n\s*\},', src, re.S)
    if m:
        keys |= set(re.findall(r"'((?:[^'\\]|\\.)*)'\s*:", m.group(1)))
    m = re.search(r'ONBOARDING_UI:\s*\[(.*?)\],', src, re.S)
    if m:
        keys |= set(re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1)))
    keys = {k.replace("\\'", "'") for k in keys}
    return keys | {b for b in (LEAD.sub('', k).strip() for k in keys) if b}


def covered(key, keys):
    if key in keys:
        return True
    body = LEAD.sub('', key).strip()
    return bool(body) and body in keys


def lint(path):
    src = open(path, encoding='utf-8').read()
    keys, skip = known_set(src), off_lines(src)
    found = {'НЕТ В НАБОРЕ': [], 'СКЛЕЙКА С ДАННЫМИ': [],
             'ЗНАЧОК В ХВОСТЕ': [], 'АТРИБУТ ВНЕ НАБОРА': [], 't() ВНЕ НАБОРА': [],
             'БЕЗ t() ПРИ ВСТАВКЕ': []}

    def judge(key, line, glued=False, attr=False):
        key = key.strip()
        if not key or not CYR.search(key):
            return
        if '":' in key.replace('\\', ''):
            return                            # схема контракта к модели, а не надпись
        if glued:
            found['СКЛЕЙКА С ДАННЫМИ'].append((line, key)); return
        if covered(key, keys):
            return
        if covered(TAIL.sub('', key).strip(), keys):
            found['ЗНАЧОК В ХВОСТЕ'].append((line, key)); return
        found['АТРИБУТ ВНЕ НАБОРА' if attr else 'НЕТ В НАБОРЕ'].append((line, key))

    for ch in scan(src):
        if ch.line in skip:
            continue
        if ch.kind == 'str':
            ctx = ch.ctx.rstrip()
            if IN_T.search(ctx) or CMP_TAIL.search(ctx):
                continue
            flat = ch.text.replace('\\', '')
            if flat.lstrip()[:2] in ('{"', ',"') or '":' in flat:
                continue                      # схема контракта, а не надпись
            if PROMPT_CTX.search(ctx) or len(ch.text) > 170:
                continue                      # промпт к модели, а не надпись интерфейса
            if not ch.inexpr and not DOM_CTX.search(ctx):
                continue                      # на экран не идёт: служебное значение или данные
            if not ch.inexpr and RAW_CTX.search(ctx):
                found['БЕЗ t() ПРИ ВСТАВКЕ'].append((ch.line, ch.text.strip()))
                continue
        if '<' in ch.text:
            nodes, attrs = split_html(ch.text)
            for x in nodes:
                judge(x, ch.line, PH in x)
            for x in attrs:
                judge(x, ch.line, PH in x, attr=True)
        else:
            if ch.kind == 'tpl' and not ch.inexpr:
                if not DOM_CTX.search(ch.ctx.rstrip()):
                    continue
                if CYR.search(ch.text) and RAW_CTX.search(ch.ctx.rstrip()):
                    found['БЕЗ t() ПРИ ВСТАВКЕ'].append((ch.line, ' '.join(ch.text.split())))
                    continue
            # шаблон или строка без единого тега: в DOM это один текстовый узел целиком
            glued = PH in ch.text or CONCAT.search(ch.ctx.rstrip()) or CONCAT_R.match(ch.tail.lstrip())
            judge(ch.text, ch.line, bool(glued))

    # 5: строки, отданные в t()/lbl(), обязаны быть и в статическом наборе — иначе
    # предзагрузка при создании профиля их не увидит
    for i, ln in enumerate(src.split('\n'), 1):
        if i in skip:
            continue
        for m in CALL.finditer(ln):
            k = m.group(1).replace("\\'", "'").strip()
            if CYR.search(k) and not covered(k, keys):
                found['t() ВНЕ НАБОРА'].append((i, k))

    total = sum(len(v) for v in found.values())
    # режим --keys: выдать недостающие ключи по одному в строке, целиком, без обрезки —
    # чтобы список можно было прямо перенести в UI_EXTRA
    if '--keys' in sys.argv:
        seen = []
        for name in ('НЕТ В НАБОРЕ', 'АТРИБУТ ВНЕ НАБОРА', 't() ВНЕ НАБОРА'):
            for _, k in sorted(found[name]):
                k = ' '.join(k.split())
                if k not in seen:
                    seen.append(k)
        for k in seen:
            print(k)
        return 1 if total else 0

    print('AILEX i18n lint — %s' % path)
    print('известных ключей: %d, строк под маркером off: %d' % (len(keys), len(skip)))
    for name in ('НЕТ В НАБОРЕ', 'СКЛЕЙКА С ДАННЫМИ', 'ЗНАЧОК В ХВОСТЕ',
                 'АТРИБУТ ВНЕ НАБОРА', 't() ВНЕ НАБОРА', 'БЕЗ t() ПРИ ВСТАВКЕ'):
        rows, seen = found[name], set()
        print('\n%s — %d' % (name, len(rows)))
        for ln, s in sorted(rows):
            if s in seen:
                continue
            seen.add(s)
            s = ' '.join(s.split())
            print('  %5d  %s' % (ln, s if len(s) <= 96 else s[:93] + '…'))
    print('\nвсего находок: %d' % total)
    return 1 if total else 0


def lint_collisions(path):
    """Столкновения имён — отдельный класс дефектов, ломающий код молча.

    Два случая, оба стоили по билду: `id="sReset"` на одном экране у двух кнопок (поиск
    возвращает первую, обработчик второй кнопки не срабатывает — удаление профиля не
    работало), и функция `langLabel`, объявленная дважды с разными сигнатурами (вторая
    перекрыла первую, и языковые пары исчезли с карточек профилей). Ни то, ни другое не
    даёт ошибки: код просто делает не то, что написано рядом.
    """
    import collections
    src = open(path, encoding='utf-8').read()
    bad = []

    # дубликаты id внутри одного шаблона экрана
    for m in re.finditer(r'Screen\.show\(`', src):
        i = m.end(); depth = 0; j = i
        while j < len(src):
            c = src[j]
            if c == '\\': j += 2; continue
            if c == '`' and depth == 0: break
            if src.startswith('${', j): depth += 1; j += 2; continue
            if c == '}' and depth > 0: depth -= 1
            j += 1
        ids = re.findall(r'\bid="([A-Za-z][\w-]*)"', src[i:j])
        dup = {k: v for k, v in collections.Counter(ids).items() if v > 1}
        if dup:
            bad.append(('дубль id на экране', src[:m.start()].count('\n') + 1, dup))

    # функции верхнего уровня, объявленные повторно
    top = [(m.group(1), src[:m.start()].count('\n') + 1)
           for m in re.finditer(r'^function\s+([A-Za-z_$][\w$]*)\s*\(', src, re.M)]
    cnt = collections.Counter(n for n, _ in top)
    for name, k in cnt.items():
        if k > 1:
            bad.append(('функция объявлена %d раза' % k, [l for n, l in top if n == name], name))

    print('\nСТОЛКНОВЕНИЯ ИМЁН — %d' % len(bad))
    for row in bad:
        print('  ' + ' | '.join(str(x) for x in row))
    return len(bad)


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'index.html'
    code = lint(path)
    if '--keys' not in sys.argv:
        code += lint_collisions(path)
    sys.exit(1 if code else 0)
