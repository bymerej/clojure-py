import re
import fractions

from clojure.lang.fileseq import FileSeq, MutatableFileSeq
from clojure.lang.var import Var, pushThreadBindings, popThreadBindings, var
from clojure.lang.ipersistentlist import IPersistentList
from clojure.lang.ipersistentvector import IPersistentVector
from clojure.lang.iseq import ISeq
from clojure.lang.ipersistentmap import IPersistentMap
from clojure.lang.ipersistentset import IPersistentSet
from clojure.lang.ipersistentcollection import IPersistentCollection
from clojure.lang.persistenthashmap import EMPTY as EMPTY_MAP
import clojure.lang.persistenthashset
from clojure.lang.cljexceptions import ReaderException, IllegalStateException
import clojure.lang.rt as RT
from clojure.lang.cljkeyword import LINE_KEY
from clojure.lang.symbol import Symbol, symbol
from clojure.lang.persistentvector import EMPTY as EMPTY_VECTOR
from clojure.lang.globals import currentCompiler
from clojure.lang.cljkeyword import Keyword, keyword
from clojure.lang.fileseq import StringReader
from clojure.lang.character import Character


def read1(rdr):
    rdr.next()
    if rdr is None:
        return ""
    return rdr.first()

_AMP_ = symbol("&")
_FN_ = symbol("fn")
_VAR_ = symbol("var")
_APPLY_ = symbol("apply")
_HASHMAP_ = symbol("clojure.core", "hashmap")
_CONCAT_ = symbol("clojure.core", "concat")
_LIST_ = symbol("clojure.core", "list")
_SEQ_ = symbol("clojure.core", "seq")
_VECTOR_ = symbol("clojure.core", "vector")
_QUOTE_ = symbol("quote")
_SYNTAX_QUOTE_ = symbol("`")
_UNQUOTE_ = symbol("~")
_UNQUOTE_SPLICING_ = symbol("~@")

ARG_ENV = var(None).setDynamic()
GENSYM_ENV = var(None).setDynamic()

WHITESPACE = [',', '\n', '\t', '\r', ' ']

symbolPat = re.compile("[:]?([\\D^/].*/)?([\\D^/][^/]*)")
intPat = re.compile(r"""
(?P<sign>[+-])?
  (:?
    # radix: 12rAA
    (?P<radix>(?P<base>[1-9][0-9]?)[rR](?P<value>[0-9a-zA-Z]+)) |
    # decima1: 0, 23, 234, 3453455
    (?P<decInt>0|[1-9][0-9]*)                                   |
    # octal: 0777
    0(?P<octInt>[0-7]+)                                         |
    # hex: 0xff
    0[xX](?P<hexInt>[0-9a-fA-F]+))
$                               # ensure the entire string matched
""", re.X)

# This floating point re has to be a bit more accurate than the original
# Clojure version because Clojure uses Double.parseDouble() to convert the
# string to a floating point value for return. If it can't convert it (for
# what ever reason), it throws.  Python float() is a lot more liberal. It's
# not a parser:
# 
# >>> float("08") => 8.0
# 
# I could just check for a decimal in matchNumber(), but is that the *only*
# case I need to check? I think it best to fully define a valid float in the
# re.
floatPat = re.compile(r"""
[+-]?
\d+
(\.\d*([eE][+-]?\d+)? |
 [eE][+-]?\d+)
$                               # ensure the entire string matched
""", re.X)

# Clojure allows what *should* be octal numbers as the numerator and
# denominator. But they are parsed as base 10 integers that allow leading
# zeros. In my opinion this isn't consistent behavior at all.
# The following re only allows base 10 integers.
ratioPat = re.compile("[-+]?(0|[1-9]+)/(0|[1-9]+)$")

def isWhitespace(c):
    return c in WHITESPACE

def isMacro(c):
    return c in macros

def isTerminatingMacro(ch):
    return ch != "#" and ch != "\'" and isMacro(ch)

def forIter(start, whileexpr, next):
    cur = start
    while whileexpr(cur):
        yield cur
        cur = next()

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
def digit(d, base = 10):
    idx = DIGITS.index(d)
    if idx == -1 or idx >= base:
        return -1
    return idx

def isDigit(d):
    return d in DIGITS and DIGITS.index(d) < 10


chrLiterals = {'t': '\t',
               'r': '\r',
               'n': '\n',
               'b': '\b',
               '\\': '\\',
               '"': '"',
               "f": '\f'}

def readString(s):
    r = StringReader(s)
    return read(r, False, None, False)

def read(rdr, eofIsError, eofValue, isRecursive):
    while True:
        ch = read1(rdr)

        while isWhitespace(ch):
            ch = read1(rdr)

        if ch == "":
            if eofIsError:
                raise ReaderException("EOF while reading", rdr)
            else:
                return eofValue

        if isDigit(ch):
            return readNumber(rdr, ch)

        m = getMacro(ch)
        if m is not None:
            ret = m(rdr, ch)
            if ret is rdr:
                continue
            return ret

        if ch in ["+", "-"]:
            ch2 = read1(rdr)
            if isDigit(ch2):
                rdr.back()
                n = readNumber(rdr, ch)
                return n
            rdr.back()

        token = readToken(rdr, ch)
        return interpretToken(token)

def unquoteReader(rdr, tilde):
    s = read1(rdr)
    if s == "":
        raise ReaderException("EOF reading unquote", rdr)
    if s == "@":
        o = read(rdr, True, None, True)
        return RT.list(_UNQUOTE_SPLICING_, o)
    else:
        rdr.back()
        o = read(rdr, True, None, True)
        return RT.list(_UNQUOTE_, o)

def isHexCharacter(ch):
    return ch in "0123456789abcdefABCDEF"

def isOctalCharacter(ch):
    return ch in "01234567"

# replaces the overloaded readUnicodeChar()
# Not really cemented to the reader
def stringCodepointToUnicodeChar(token, offset, length, base):
    """Return a unicode character given a string that specifies a codepoint.

    token -- string to parse
    offset -- index into token where codepoint starts
    length -- maximum number of digits to read from offset
    base -- expected radix of the codepoint

    Return a unicode string of length one."""
    if len(token) != offset + length:
        raise UnicodeError("Invalid unicode character: \\%s" % token)
    try:
        return unichr(int(token[offset:], base))
    except:
        raise UnicodeError("Invalid unicode character: \\%s" % token)

def readUnicodeChar(rdr, initch, base, length, exact):
    """Read a string that specifies a Unicode codepoint.

    rdr -- read/unread-able object
    initch -- the first character of the codepoint string
    base -- expected radix of the codepoint
    length -- maximum number of characters in the codepoint
    exact -- if True, codepoint string must contain length characters
             if False, it must contain [1, length], inclusive

    Return a unicode string of length one."""
    digits = []
    try:
        int(initch, base)
        digits += initch
    except ValueError:
        raise ReaderException("Expected base %d digit, got: %s"
                              % (base, initch), rdr)
    for i in range(1, length):
        ch = read1(rdr)
        if ch == "" or isWhitespace(ch) or isMacro(ch):
            rdr.back()
            break
        try:
            int(ch, base)
            digits += ch
        except ValueError:
            if exact:
                raise ReaderException("Expected base %d digit, got: %s"
                                      % (base, ch), rdr)
            else:
                rdr.back()
                break
    if i != length-1 and exact:
        raise ReaderException("Invalid character length: %d, should be: %d"
                              % (i, length), rdr)
    return unichr(int("".join(digits), base))

tokenMappings = {"newline": "\n",
                 "space": " ",
                 "tab": "\t",    
                 "backspace": "\b",    
                 "formfeed": "\f",    
                 "return": "\r"}

def characterReader(rdr, backslash):
    """Read a single clojure-py formatted character from r.

    Return a Character instance."""
    ch = rdr.read()
    if ch == "":
        raise Exception("EOF while reading character")
    token = readToken(rdr, ch)
    if len(token) == 1:
        return Character(token)
    elif token in tokenMappings:
        return Character(tokenMappings[token])
    elif token.startswith("u"):
        try:
            ch = stringCodepointToUnicodeChar(token, 1, 4, 16)
        except UnicodeError as e:
            raise ReaderException(e.args[0], rdr)
        codepoint = ord(ch)
        if u"\ud800" <= ch <= u"\udfff":
            raise Exception("Invalid character constant in literal string:"
                            " \\%s" % token)
        return ch
    elif token.startswith("o"):
        if len(token) > 4:
            raise Exception("Invalid octal escape sequence length in literal"
                            " string. Three digits max: \\%s" % token)
        try:
            ch = stringCodepointToUnicodeChar(token, 1, len(token) - 1, 8)
        except UnicodeError as e:
            raise ReaderException(e.args[0], rdr)
        codepoint = ord(ch)
        if codepoint > 255:
            raise Exception("Octal escape sequence in literal string"
                            " must be in range [0, 377], got: \\o%o"
                            % codepoint)
        return ch
    raise Exception("Unsupported character: \\" + token)


def stringReader(rdr, doublequote):
    """Read a double-quoted \"\" literal string.

    Return a str or unicode object."""
    buf = []
    ch = read1(rdr)
    while True:
        if ch == "":
            raise ReaderException("EOF while reading string")
        if ch == '\\':
            ch = read1(rdr)
            if ch == "":
                raise ReaderException("EOF while reading string")
            elif ch in chrLiterals:
                ch = chrLiterals[ch]
            elif ch == "u":
                ch = read1(rdr)
                if not isHexCharacter(ch):
                    raise ReaderException("Hexidecimal digit expected after"
                                          " \\u in literal string, got: %s"
                                          % ch, rdr)
                ch = readUnicodeChar(rdr, ch, 16, 4, True)
            elif ch == "U":
                ch = read1(rdr)
                if not isHexCharacter(ch):
                    raise ReaderException("Hexidecimal digit expected after"
                                          " \\u in literal string, got: %s"
                                          % ch, rdr)
                ch = readUnicodeChar(rdr, ch, 16, 8, True)
            elif isOctalCharacter(ch):
                ch = readUnicodeChar(rdr, ch, 8, 3, False)
                if ord(ch) > 255:
                    raise ReaderException("Octal escape sequence in literal"
                                          " string must be in range [0, 377]"
                                          ", got: %o" % ord(ch))
            else:
                raise ReaderException("Unsupported escape character in"
                                      " literal string: \\%s" % ch, rdr)
        elif ch == '"':
            return "".join(buf)
        buf += ch
        ch = read1(rdr)
    
def readToken(rdr, initch):
    sb = [initch]
    while True:
        ch = read1(rdr)
        if ch == "" or isWhitespace(ch) or isTerminatingMacro(ch):
            rdr.back()
            break
        sb.append(ch)
    s = "".join(sb)
    return s

INTERPRET_TOKENS = {"nil": None,
                    "true": True,
                    "false": False}
def interpretToken(s):
    if s in INTERPRET_TOKENS:
        return INTERPRET_TOKENS[s]
    ret = matchSymbol(s)
    if ret is None:
        raise ReaderException("Unknown symbol " + str(s))
    return ret

def readNumber(rdr, initch):
    sb = [initch]
    while True:
        ch = read1(rdr)
        if ch == "" or isWhitespace(ch) or isMacro(ch):
            rdr.back()
            break
        sb.append(ch)

    s = "".join(sb)
    try:
        n = matchNumber(s)
    except Exception as e:
        raise ReaderException(e.args[0], rdr)
    if n is None:
        raise ReaderException("Invalid number: " + s, rdr)
    return n

def matchNumber(s):
    """Find if the string s is a valid literal number.

    Return the numeric value of s if so, else return None."""
    mo = intPat.match(s)
    if mo:
        mogd = mo.groupdict()
        sign = mogd["sign"] or "+"
        # 12rAA
        if mogd["radix"]:
            return int(sign + mogd["value"], int(mogd["base"], 10))
        # 232
        elif mogd["decInt"]:
            return int(sign + mogd["decInt"])
        # 0777
        elif mogd["octInt"]:
            return int(sign + mogd["octInt"], 8)
        # 0xdeadbeef
        elif mogd["hexInt"]:
            return int(sign + mogd["hexInt"], 16)
    # 1e3, 0.3,
    mo = floatPat.match(s)
    if mo:
        return float(mo.group())
    # 1/2
    mo = ratioPat.match(s)
    if mo:
        return fractions.Fraction(mo.group())
    else:
        return None

def getMacro(ch):
    return macros[ch] if ch in macros else None

def commentReader(rdr, semicolon):
    while True:
        chr = read1(rdr)
        if chr == "" or chr == '\n' or chr == '\r':
            break
    return rdr

def discardReader(rdr, underscore):
    read(rdr, True, None, True)
    return rdr


class wrappingReader(object):
    def __init__(self, sym):
        self.sym = sym

    def __call__(self, rdr, quote):
        o = read(rdr, True, None, True)
        return RT.list(self.sym, o)


def varReader():
    return wrappingReader(THE_VAR)#FIXME: THE_VAR undefined

def dispatchReader(rdr, hash):
    ch = read1(rdr)
    if ch == "":
        raise ReaderException("EOF while reading character")
    if ch not in dispatchMacros:
        raise ReaderException("No dispatch macro for: ("+ ch + ")")
    return dispatchMacros[ch](rdr, ch)

def listReader(rdr, leftparen):
    startline = rdr.lineCol()[0]
    lst = readDelimitedList(')', rdr, True)
    lst = apply(RT.list, lst)
    return lst.withMeta(RT.map(LINE_KEY, startline))

def vectorReader(rdr, leftbracket):
    startline = rdr.lineCol()[0]
    lst = readDelimitedList(']', rdr, True)
    lst = apply(RT.vector, lst)
    return lst

def mapReader(rdr, leftbrace):
    startline = rdr.lineCol()[0]
    lst = readDelimitedList('}', rdr, True)
    lst = apply(RT.map, lst)
    return lst

def unmatchedDelimiterReader(rdr, un):
    raise ReaderException("Unmatched Delimiter " + un + " at " + str(rdr.lineCol()))

def readDelimitedList(delim, rdr, isRecursive):
    firstline = rdr.lineCol()[0]
    a = []

    while True:
        ch = read1(rdr)
        while isWhitespace(ch):
            ch = read1(rdr)
        if ch == "":
            raise ReaderException("EOF while reading starting at line " + str(firstline))

        if ch == delim:
            break

        macrofn = getMacro(ch)
        if macrofn is not None:
            mret = macrofn(rdr, ch)
            if mret is not None and mret is not rdr:
                a.append(mret)
        else:
            rdr.back()
            o = read(rdr, True, None, isRecursive)
            a.append(o)

    return a

def regexReader(rdr, doubleQuote):
    s = []
    ch = -1
    while ch != '\"':
        ch = read1(rdr)
        if ch == "":
            raise ReaderException("EOF while reading string", rdr)
        s.append(ch)
        if ch == "\\":
            ch = read1(rdr)
            if ch == "":
                raise ReaderException("EOF while reading regex", rdr)
            s.append(ch)
    
    return re.compile("".join(s))

def metaReader(rdr, caret):
    from clojure.lang.symbol import Symbol
    from clojure.lang.cljkeyword import Keyword, TAG_KEY, T
    from clojure.lang.ipersistentmap import IPersistentMap
    line = rdr.lineCol()[0]
    meta = read(rdr, True, None, True)
    if isinstance(meta, Symbol) or isinstance(meta, str):
        meta = RT.map(TAG_KEY, meta)
    elif isinstance(meta, Keyword):
        meta = RT.map(meta, T)
    elif not isinstance(meta, IPersistentMap):
        raise ReaderException("Metadata must be Symbol,Keyword,String or Map")
    o = read(rdr, True, None, True)
    if not hasattr(o, "withMeta"):
        raise ReaderException("Cannot attach meta to a object without .withMeta")
    return o.withMeta(meta)

def matchSymbol(s):
    from clojure.lang.symbol import Symbol
    from clojure.lang.cljkeyword import Keyword
    m = symbolPat.match(s)
    if m is not None:
        ns = m.group(1)
        name = m.group(2)
        if name.endswith(".") and not name.startswith("."):
            name = name[:-1]
        if ns is not None and ns.endswith(":/") or name.endswith(":")\
            or s.find("::") != -1:
                return None
        if s.startswith("::"):
            return "FIX"
        ns = ns if ns is None else ns[:-1]
        iskeyword = s.startswith(':')
        if iskeyword:
            return keyword(s[1:])
        else:
            return symbol(ns, name)
    return None


    
def setReader(rdr, leftbrace):
    from persistenthashset import create
    return create(readDelimitedList("}", rdr,  True))

def argReader(rdr, perc):
    if ARG_ENV.deref() is None:
        return interpretToken(readToken(rdr, '%'))
    ch = read1(rdr)
    rdr.back()
    if ch == "" or isWhitespace(ch) or isTerminatingMacro(ch):
        return registerArg(1)
    n = read(rdr, True, None, True)
    if isinstance(n, Symbol) and n == _AMP_:
        return registerArg(-1)
    if not isinstance(n, int):
        raise IllegalStateException("arg literal must be %, %& or %integer")
    return registerArg(n)

def varQuoteReader(rdr, singlequote):
    line = rdr.lineCol()[0]
    form = read(rdr, True, None, True)
    return RT.list(_VAR_, form).withMeta(RT.map(LINE_KEY, line))

def registerArg(arg):
    argsyms = ARG_ENV.deref()
    if argsyms is None:
        raise IllegalStateException("arg literal not in #()")
    ret = argsyms[arg]
    if ret is None:
        ret = garg(arg)
        ARG_ENV.set(argsyms.assoc(arg, ret))
    return ret

def fnReader(rdr, lparen):
    from clojure.lang.persistenthashmap import EMPTY
    from clojure.lang.var import popThreadBindings, pushThreadBindings

    if ARG_ENV.deref() is not None:
        raise IllegalStateException("Nested #()s are not allowed")
    pushThreadBindings(RT.map(ARG_ENV, EMPTY))
    rdr.back()
    form = read(rdr, True, None, True)
    drefed = ARG_ENV.deref()
    sargs = sorted(list(filter(lambda x: x != -1, drefed)))
    args = []
    if len(sargs):
        for x in range(1, int(str(sargs[-1])) + 1):
            if x in drefed:
                args.append(drefed[x])
            else:
                args.append(garg(x))
        retsym = drefed[-1]
        if retsym is not None:
            args.append(_AMP_)
            args.append(retsym)

    vargs = RT.vector(*args)
    popThreadBindings()
    return RT.list(_FN_, vargs, form)

def isUnquote(form):
    return isinstance(form, ISeq) and form.first() == _UNQUOTE_

def isUnquoteSplicing(form):
    return isinstance(form, ISeq) and form.first() == _UNQUOTE_SPLICING_

class SyntaxQuoteReader(object):
    def __call__(self, r, backquote):
        pushThreadBindings(RT.map(GENSYM_ENV, EMPTY_MAP))
        try:
            self.rdr = r
            form = read(r, True, None, True)
            return self.syntaxQuote(form)
        finally:
            popThreadBindings()

    def syntaxQuote(self, form):
        from clojure.lang.compiler import builtins as compilerbuiltins

        if form in compilerbuiltins:
            ret = RT.list(_QUOTE_, form)
        elif isinstance(form, Symbol):
            sym = form
            if sym.ns is None and sym.name.endswith("#"):
                gmap = GENSYM_ENV.deref()
                if gmap == None:
                    raise ReaderException("Gensym literal not in syntax-quote, before", self.rdr)
                gs = gmap[sym]
                if gs is None:
                    gs = symbol(None, sym.name[:-1] + "__" + str(RT.nextID()) + "__auto__")
                    GENSYM_ENV.set(gmap.assoc(sym, gs))
                sym = gs
            elif sym.ns is None and sym.name.endswith("."):
                ret = sym
            elif sym.ns is None and sym.name.startswith("."):
                ret = sym
            elif sym.ns is not None:
                ret = sym

            else:
                comp = currentCompiler.get(lambda: None)
                if comp is None:
                    raise IllegalStateException("No Compiler found in syntax quote!")
                ns = comp.getNS()
                if ns is None:
                    raise IllegalStateException("No ns in reader")
                sym = symbol(ns.__name__, sym.name)
            ret = RT.list(_QUOTE_, sym)
        else:
            if isUnquote(form):
                return form.next().first()
            elif isUnquoteSplicing(form):
                raise IllegalStateException("splice not in list")
            elif isinstance(form, IPersistentCollection):
                if isinstance(form, IPersistentMap):
                    keyvals = self.flattenMap(form)
                    ret = RT.list(_APPLY_, _HASHMAP_, RT.list(RT.cons(_CONCAT_, self.sqExpandList(keyvals.seq()))))
                elif isinstance(form, (IPersistentVector, IPersistentSet)):
                    ret = RT.list(_APPLY_, _VECTOR_, RT.list(_SEQ_, RT.cons(_CONCAT_, self.sqExpandList(form.seq()))))
                elif isinstance(form, (ISeq, IPersistentList)):
                    seq = form.seq()
                    if seq is None:
                        ret = RT.cons(_LIST_, None)
                    else:
                        ret = RT.list(_SEQ_, RT.cons(_CONCAT_, self.sqExpandList(seq)))
                else:
                    raise IllegalStateException("Unknown collection type")
            elif isinstance(form, (int, float, str, Keyword)):
                ret = form
            else:
                ret = RT.list(_QUOTE_, form)
        if hasattr(form, "meta") and form.meta() is not None:
            newMeta = form.meta().without(LINE_KEY)
            if len(newMeta) > 0:
                return RT.list(_WITH_META_, ret, self.syntaxQuote(form.meta()))#FIXME: _WITH_META_ undefined
        return ret

    def sqExpandList(self, seq):
        ret = EMPTY_VECTOR
        while seq is not None:
            item = seq.first()
            if isUnquote(item):
                ret = ret.cons(RT.list(_LIST_, item.next().first()))
            elif isUnquoteSplicing(item):
                ret = ret.cons(item.next().first())
            else:
                ret = ret.cons(RT.list(_LIST_, self.syntaxQuote(item)))
            seq = seq.next()
        return ret.seq()

    def flattenMap(self, m):
        keyvals = EMPTY_VECTOR
        s = form.seq()#FIXME: undefined 'form'
        while s is not None:
            e = s.first()
            keyvals = keyvals.cons(e.getKey())
            keyvals = keyvals.cons(e.getVal())
            s = s.next()
        return keyvals


def garg(n):
    from symbol import Symbol
    return symbol(None,  "rest" if n == -1 else  ("p" + str(n)) + "__" + str(RT.nextID()) + "#")

macros = {'\"': stringReader,
          "\'": wrappingReader(_QUOTE_),
          "(": listReader,
          ")": unmatchedDelimiterReader,
          "[": vectorReader,
          "]": unmatchedDelimiterReader,
          "{": mapReader,
          "}": unmatchedDelimiterReader,
          ";": commentReader,
          "#": dispatchReader,
          "^": metaReader,
          "%": argReader,
          "`": SyntaxQuoteReader(),
          "~": unquoteReader,
          "\\": characterReader}  

dispatchMacros = {"\"": regexReader,
                  "{": setReader,
                  "!": commentReader,
                  "_": discardReader,
                  "(": fnReader,
                  "'": varQuoteReader,
                  "^": metaReader}
