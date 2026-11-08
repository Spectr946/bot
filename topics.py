import unicodedata

def line_text(line):
    line = unicodedata.normalize("NFKC", line)
    parts = line.rsplit(None, 1)
    if len(parts) == 2 and parts[-1].isdigit() and len(parts[-1]) <= 4:
        if ".." in parts[0] or "…" in parts[0] or "··" in parts[0] or "  " in line:
            line = parts[0].rstrip(" .…·")
    line = " ".join(line.split()).strip(" \ufeff•▪")
    position = line.lower().find("grammar reference")
    if position >= 0:
        line = line[:position].rstrip(" >»")
        if line.lower().endswith(("pr", "yp")):
            line = line[:-2].rstrip()
    parts = line.rsplit(maxsplit=1)
    if len(parts) == 2 and not parts[-1].strip("/\\|,–—-"):
        line = parts[0]
    if line.lower().endswith(" key word transformations"):
        line = line[:-25]
    if "word formation:" in line.lower() and " phrasal verbs:" in line.lower():
        line = line[:line.lower().index(" phrasal verbs:")]
    return line

def numbered_title(line):
    parts = line.split(maxsplit=1)
    if len(parts) < 2:
        return None
    number = parts[0].rstrip(".)")
    if number[-1:].lower() in "abcdefghijklmnopqrstuvwxyz" and len(number) > 1:
        number = number[:-1]
    for part in number.split("."):
        if not part.isdigit():
            return None
    return parts[1]

def section_title(line):
    parts = line.split(maxsplit=1)
    if not parts:
        return None
    if line.startswith("§"):
        rest = line[1:].strip()
    elif parts[0].lower() in ("глава", "раздел", "тема", "параграф", "module", "unit", "topic", "lesson", "chapter", "section"):
        rest = parts[1] if len(parts) == 2 else ""
    else:
        return None
    parts = rest.split(maxsplit=1)
    if not parts:
        return None
    number = parts[0].rstrip(".):-")
    arabic = number
    if arabic[-1:].lower() in "abcdefghijklmnopqrstuvwxyz" and len(arabic) > 1:
        arabic = arabic[:-1]
    valid_number = True
    for part in arabic.split("."):
        if not part.isdigit():
            valid_number = False
    if not valid_number:
        if not number:
            return None
        for char in number.lower():
            if char not in "ivxlcdm":
                return None
    return parts[1] if len(parts) == 2 else ""

def heading_category(line):
    lower = line.lower()
    for name in ("grammar in use", "culture corner", "across the curriculum",
                 "grammar", "vocabulary", "reading", "listening", "speaking", "writing"):
        if lower != name and not lower.startswith((name + " ", name + ":")):
            continue
        end = len(name)
        if name in ("reading", "listening", "speaking", "writing"):
            rest = lower[end:]
            if rest.startswith(" & "):
                other = rest[3:].split(maxsplit=1)[0]
                if other in ("reading", "listening", "speaking", "writing"):
                    end += len(other) + 3
            if lower[end:].startswith(" skills"):
                end += 7
        return line[:end]
    return ""

def title_text(line):
    title = section_title(line)
    if title is None:
        title = line
    numbered = numbered_title(title)
    if numbered is not None:
        title = numbered
    return title.strip(" .:–—-")

def heading_key(line):
    title = title_text(line)
    category = heading_category(title)
    if category and len(category) < len(title):
        title = title[len(category):].strip(" :–—-")
    title = title.casefold().replace("ё", "е")
    cleaned = ""
    for char in title:
        if char.isalnum() or char == "_":
            cleaned += char
        else:
            cleaned += " "
    return " ".join(cleaned.split())

def uppercase_title(line):
    letters = ""
    for char in line:
        if char.isalpha():
            letters += char
    return len(letters) >= 3 and letters.isupper()

def short_heading(line, allow_single=False, partial=False):
    title = title_text(line)
    text = ""
    for char in title:
        if char.isalpha() or char in "’'-":
            text += char
        else:
            text += " "
    words = text.split()
    if not 3 <= len(line) <= 120 or not 1 <= len(words) <= 13:
        return False
    if len(words) == 1 and not allow_single:
        return False
    if line.endswith((".", ":", ",")):
        return False
    for char in title:
        if char in "!?;=|{}<>_":
            return False
    for word in title.split()[:-1]:
        if len(word) == 2 and word[0] in "ABCDEFGHАБВГ" and word[-1] in ".)":
            return False
    lower = title.casefold()
    for junk in ("http://", "https://", "www.", "@", "©", "isbn", "copyright",
                 "all rights reserved", "published", "publishing", "publisher", "printed in",
                 "photocopi", "student's book", "student’s book", "teacher's book",
                 "teacher’s book", "workbook", "учебник", "издатель", "авторы", "автора",
                 "правообладател", "тираж", "учебное издание"):
        if junk in lower:
            return False
    if lower == "автор" or lower.startswith("автор "):
        return False
    category = heading_category(title)
    instruction = title[len(category):].strip(" :–—-") if category else title
    if instruction.startswith("(") or category == title or lower.startswith("grammar check"):
        return False
    parts = instruction.lower().split()
    if parts and parts[0].rstrip(".)") in "abcdefghабвг" and len(parts[0].rstrip(".)")) == 1:
        parts = parts[1:]
    if parts and parts[0] in (
            "назовите", "объясните", "ответьте", "запишите", "решите", "выполните", "докажите",
            "сравните", "опишите", "рассмотрите", "укажите", "проверьте", "почему", "как", "что",
            "прочитайте", "найдите", "подумайте", "заполните", "выберите", "составьте",
            "read", "listen", "write", "match", "fill", "complete", "choose", "circle", "answer",
            "underline", "look", "think", "discuss", "ask", "work", "check", "find", "correct",
            "put", "use", "talk", "copy", "translate", "make", "try", "compare", "explain",
            "describe", "identify", "say", "repeat", "take", "act", "tell", "tick", "label",
            "speak", "decide", "give", "watch", "do", "continue", "form", "imagine", "portfolio",
            "what", "where", "when", "why", "how", "which", "who"):
        return False
    if parts[:2] in (["can", "you"], ["your", "turn"]):
        return False
    full_key = ""
    for char in lower:
        full_key += char if char.isalnum() else " "
    full_key = " ".join(full_key.split())
    for key in (heading_key(line), full_key):
        if key in (
                "содержание", "оглавление", "вопросы", "задания", "упражнения", "проверьте себя",
                "введение", "заключение", "ответ", "ответы", "правильный ответ", "контрольные вопросы",
                "проверьте знания", "самостоятельная работа", "contents", "table of contents",
                "introduction", "acknowledgements", "answer key", "answers", "revision", "self check",
                "self assessment", "progress check", "check your progress", "study skills",
                "learning objectives", "useful language", "portfolio", "project", "projects",
                "word list", "wordlist", "irregular verbs", "grammar reference", "further practice",
                "suggested answer", "suggested answers", "possible answers", "key vocabulary",
                "vocabulary bank", "spotlight", "spotlight 10", "virginia evans", "jenny dooley",
                "bob obee", "grammar in use", "grammar check", "everyday english", "say it right",
                "reading aloud", "reading for specific information", "understanding rubrics",
                "checking editing your work", "endings", "beginnings endings", "concluding", "recommending"):
            return False
    if not partial and title.lower().split()[-1] in ("to", "the", "a", "of", "and", "is", "with", "as", "your", "from", "or", "but"):
        return False
    name_parts = title.replace(". ", ".").split(".")
    if len(name_parts) > 1:
        initials = True
        for part in name_parts[:-1]:
            if len(part) != 1 or not part.isupper():
                initials = False
        if initials:
            return False
    short_words = 0
    for word in words:
        if len(word) <= 2:
            short_words += 1
    if len(words) > 2 and short_words > len(words) / 2:
        return False
    letters = "".join(words).lower()
    if len(letters) < len("".join(title.split())) * 0.7:
        return False
    for char in letters:
        if char * 4 in letters:
            return False
    for char in letters:
        if char in "aeiouyаеёиоуыэюя":
            return True
    return False

def continues_heading(previous, following):
    if not following or section_title(following) is not None or numbered_title(following) is not None or heading_category(following):
        return False
    if not short_heading(following, allow_single=True, partial=True) or len(following.split()) > 7:
        return False
    if heading_category(previous) == previous and uppercase_title(following) and len(following.split()) == 1:
        return False
    if section_title(previous) == "" or heading_category(previous) == previous:
        return True
    if previous.endswith("-") or uppercase_title(previous) and uppercase_title(following):
        return True
    return following[0].islower() and not previous.endswith((".", ":"))

def clean_topics(candidates, validated_ids=None):
    if not isinstance(candidates, (list, tuple)):
        return []
    allowed = None
    if validated_ids is not None:
        allowed = []
        for value in validated_ids:
            if type(value) is int and 0 <= value < len(candidates):
                allowed.append(value)
    result = []
    seen = set()
    for index, candidate in enumerate(candidates):
        if allowed is not None and index not in allowed:
            continue
        if not isinstance(candidate, str):
            continue
        heading = line_text(candidate)
        if not short_heading(heading, allow_single=section_title(heading) is not None):
            continue
        key = heading_key(heading)
        if key not in seen:
            result.append(heading)
            seen.add(key)
        if len(result) == 40:
            break
    return result

def find_topics(text):
    if not isinstance(text, str) or not text.strip():
        return []
    lines = []
    for line in text.replace("\r", "\n").split("\n"):
        lines.append(line_text(line))
    candidates = []
    index = 0
    while index < len(lines):
        line = lines[index]
        section = section_title(line) is not None
        category = heading_category(line)
        bare = section_title(line) == "" or bool(category and category == line)
        possible_wrap = index + 1 < len(lines) and continues_heading(line, lines[index + 1])
        if not bare and not short_heading(line, allow_single=section or possible_wrap, partial=possible_wrap):
            index += 1
            continue
        before_blank = index == 0 or not lines[index - 1]
        after_blank = index + 1 == len(lines) or not lines[index + 1]
        numbered = False
        if line:
            number = line.split()[0].split(".")
            numbered = len(number) > 1
            for part in number:
                if not part.isdigit():
                    numbered = False
        wrapped = before_blank and possible_wrap and (index + 2 == len(lines) or not lines[index + 2])
        standalone = before_blank and after_blank and line[:1].isupper() and len(line.split()) <= 8
        looks_like_heading = section or bool(category) or uppercase_title(line) or wrapped
        if numbered and before_blank:
            looks_like_heading = True
        if standalone and numbered_title(line) is None:
            looks_like_heading = True
        if not looks_like_heading:
            index += 1
            continue
        heading = line
        end = index + 1
        if bare and line.lower() in ("grammar", "grammar in use", "vocabulary", "writing", "writing skills"):
            while end < min(index + 4, len(lines)) and not lines[end]:
                end += 1
        if section or category or uppercase_title(line) or wrapped:
            while end < min(index + 5, len(lines)):
                following = lines[end]
                if not continues_heading(heading, following) or len(heading) + len(following) + 1 > 120:
                    break
                heading = heading[:-1] + following if heading.endswith("-") else heading + " " + following
                end += 1
        if short_heading(heading, allow_single=section):
            priority = 3
            if section:
                priority = 0
            elif numbered or category and (line.lower().startswith(("vocabulary", "grammar in use")) or bare and line.lower() == "writing skills"):
                priority = 1
            elif category:
                priority = 2
            candidates.append((priority, index, heading))
        index = end
    strong = []
    for candidate in candidates:
        if candidate[0] < 3:
            strong.append(candidate)
    ordered = sorted(candidates)
    if len(strong) >= 5:
        ordered = sorted(strong)
    headings = []
    for priority, position, heading in ordered:
        headings.append(heading)
    selected = clean_topics(headings)
    result = []
    for _, _, heading in candidates:
        if heading in selected and heading not in result:
            result.append(heading)
    return result
