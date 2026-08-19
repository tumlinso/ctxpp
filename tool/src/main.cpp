#include "ctxpp/clang_abi.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace fs = std::filesystem;
using ctxpp::clangabi::Api;
using ctxpp::clangabi::CXChildVisitResult;
using ctxpp::clangabi::CXClientData;
using ctxpp::clangabi::CXCursor;
using ctxpp::clangabi::CXFile;
using ctxpp::clangabi::CXIndex;
using ctxpp::clangabi::CXSourceLocation;
using ctxpp::clangabi::CXSourceRange;
using ctxpp::clangabi::CXToken;
using ctxpp::clangabi::CXTranslationUnit;

namespace {

std::string json(std::string_view s) {
  std::ostringstream out;
  out << '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\b': out << "\\b"; break;
      case '\f': out << "\\f"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default:
        if (c < 0x20) {
          static const char* h = "0123456789abcdef";
          out << "\\u00" << h[c >> 4] << h[c & 15];
        } else out << static_cast<char>(c);
    }
  }
  out << '"';
  return out.str();
}

std::string take(Api& a, ctxpp::clangabi::CXString value) {
  const char* p = a.getCString(value);
  std::string result = p ? p : "";
  a.disposeString(value);
  return result;
}

struct Loc { std::string file; unsigned line{}, column{}, offset{}; };

Loc location(Api& a, ctxpp::clangabi::CXSourceLocation loc) {
  CXFile f{}; Loc out;
  a.getSpellingLocation(loc, &f, &out.line, &out.column, &out.offset);
  if (f) out.file = take(a, a.getFileName(f));
  return out;
}

struct Range { Loc begin, end; };
Range range(Api& a, CXSourceRange r) { return {location(a, a.getRangeStart(r)), location(a, a.getRangeEnd(r))}; }

std::string normalize(const fs::path& p) {
  std::error_code ec;
  fs::path q = fs::weakly_canonical(p, ec);
  return (ec ? p.lexically_normal() : q).generic_string();
}

std::string relative_to(std::string path, const std::string& root) {
  std::error_code ec;
  fs::path rel = fs::relative(fs::path(path), fs::path(root), ec);
  if (!ec && !rel.empty() && rel.native().rfind("..", 0) != 0) return rel.generic_string();
  return fs::path(path).generic_string();
}

std::string read_file(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot read " + path);
  return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

std::string compact_space(std::string_view s) {
  std::string out; bool ws = false;
  for (unsigned char c : s) {
    if (std::isspace(c)) { ws = !out.empty(); continue; }
    if (ws) out.push_back(' ');
    out.push_back(static_cast<char>(c)); ws = false;
  }
  return out;
}

struct Record { std::string key; std::string line; };

struct ScanContext {
  Api* api{};
  CXTranslationUnit tu{};
  std::string root;
  std::string main_file;
  std::string source;
  std::vector<Record> records;
  std::vector<std::string> parents;
  std::map<std::string, std::vector<std::string>> declarations_by_name;
};

void inclusion(CXFile included, CXSourceLocation* stack, unsigned length, CXClientData data) {
  if (!included || !stack || !length) return;
  auto& c = *static_cast<ScanContext*>(data);
  CXFile including{}; unsigned line = 0, column = 0, offset = 0;
  c.api->getSpellingLocation(stack[0], &including, &line, &column, &offset);
  if (!including) return;
  const std::string canonical_from = normalize(take(*c.api, c.api->getFileName(including)));
  const std::string canonical_to = normalize(take(*c.api, c.api->getFileName(included)));
  auto in_root = [&c](const std::string& path) { return path == c.root || path.rfind(c.root + "/", 0) == 0; };
  if (!in_root(canonical_from) || !in_root(canonical_to)) return;
  const std::string from = relative_to(canonical_from, c.root);
  const std::string to = relative_to(canonical_to, c.root);
  std::ostringstream record;
  record << "{\"record\":\"include\",\"from\":" << json(from) << ",\"to\":" << json(to)
         << ",\"line\":" << line << "}";
  c.records.push_back({"I\t" + from + "\t" + to + "\t" + std::to_string(line), record.str()});
}

std::string cursor_usr(ScanContext& c, CXCursor cursor) { return take(*c.api, c.api->getCursorUSR(cursor)); }

std::string qualified_name(ScanContext& c, CXCursor cursor) {
  std::vector<std::string> names;
  for (CXCursor cur = cursor; !c.api->Cursor_isNull(cur); cur = c.api->getCursorSemanticParent(cur)) {
    std::string n = take(*c.api, c.api->getCursorSpelling(cur));
    std::string k = take(*c.api, c.api->getCursorKindSpelling(cur.kind));
    if (!n.empty() && k != "TranslationUnit") names.push_back(n);
  }
  std::reverse(names.begin(), names.end());
  std::ostringstream out;
  for (std::size_t i = 0; i < names.size(); ++i) { if (i) out << "::"; out << names[i]; }
  return out.str();
}

std::string nearest_parent_usr(ScanContext& c, CXCursor cursor) {
  for (CXCursor cur = c.api->getCursorSemanticParent(cursor); !c.api->Cursor_isNull(cur); cur = c.api->getCursorSemanticParent(cur)) {
    std::string u = cursor_usr(c, cur);
    if (!u.empty()) return u;
  }
  return {};
}

unsigned token_count(ScanContext& c, CXSourceRange extent) {
  CXToken* tokens = nullptr; unsigned n = 0;
  c.api->tokenize(c.tu, extent, &tokens, &n);
  if (tokens) c.api->disposeTokens(c.tu, tokens, n);
  return n;
}

bool is_header(std::string_view p) {
  const std::string ext = fs::path(p).extension().string();
  return ext == ".h" || ext == ".hh" || ext == ".hpp" || ext == ".hxx" || ext == ".cuh";
}

CXChildVisitResult visit(CXCursor cursor, CXCursor, void* data) {
  auto& c = *static_cast<ScanContext*>(data);
  Api& a = *c.api;
  Loc loc = location(a, a.getCursorLocation(cursor));
  if (loc.file.empty()) return ctxpp::clangabi::Recurse;
  const std::string canonical = normalize(loc.file);
  if (canonical != c.root && canonical.rfind(c.root + "/", 0) != 0) return ctxpp::clangabi::Recurse;
  const std::string rel = relative_to(canonical, c.root);
  if (rel.rfind("..", 0) == 0) return ctxpp::clangabi::Recurse;

  const std::string kind = take(a, a.getCursorKindSpelling(cursor.kind));
  const Range rr = range(a, a.getCursorExtent(cursor));
  const std::string usr = cursor_usr(c, cursor);
  const std::string name = take(a, a.getCursorSpelling(cursor));
  std::string lower_kind = kind;
  std::transform(lower_kind.begin(), lower_kind.end(), lower_kind.begin(), [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });

  if (lower_kind == "macro definition" && !name.empty()) {
    const std::string macro_id = usr.empty() ? "macro:" + name + "@" + rel + ":" + std::to_string(rr.begin.offset) : usr;
    unsigned macro_start = rr.begin.offset, macro_end = rr.end.offset;
    std::string macro_source;
    try { macro_source = canonical == c.main_file ? c.source : read_file(canonical); } catch (const std::exception&) {}
    if (!macro_source.empty()) {
      std::size_t bol = macro_source.rfind('\n', macro_start);
      macro_start = bol == std::string::npos ? 0U : static_cast<unsigned>(bol + 1);
      std::size_t eol = macro_source.find('\n', macro_end);
      macro_end = static_cast<unsigned>(eol == std::string::npos ? macro_source.size() : eol);
    }
    std::ostringstream line;
    line << "{\"record\":\"symbol\",\"id\":" << json(macro_id) << ",\"name\":" << json(name)
         << ",\"qualified_name\":" << json(name) << ",\"kind\":\"MacroDefinition\",\"file\":" << json(rel)
         << ",\"start\":" << macro_start << ",\"end\":" << macro_end << ",\"line\":" << rr.begin.line
         << ",\"column\":" << rr.begin.column << ",\"end_line\":" << rr.end.line << ",\"end_column\":" << rr.end.column
         << ",\"name_start\":" << loc.offset << ",\"name_end\":" << (loc.offset + name.size())
         << ",\"definition\":true,\"signature\":" << json(macro_source.empty() ? name : compact_space(std::string_view(macro_source).substr(macro_start, macro_end - macro_start)))
         << ",\"type\":\"macro\",\"tokens\":"
         << token_count(c, a.getCursorExtent(cursor)) << ",\"parent_id\":\"\",\"linkage\":0,\"visibility\":0,\"access\":0"
         << ",\"public\":true,\"body_required\":true,\"template\":false,\"inline\":false,\"constexpr\":false"
         << ",\"cuda\":false,\"macro_related\":true,\"contract\":\"\"}";
    c.records.push_back({"S\t" + macro_id, line.str()});
    c.declarations_by_name[name].push_back(macro_id);
  }

  if (a.isDeclaration(cursor.kind) && !usr.empty() && !name.empty()) {
    unsigned start = rr.begin.offset, end = rr.end.offset;
    std::string signature;
    std::string declaration_source;
    try { declaration_source = canonical == c.main_file ? c.source : read_file(canonical); }
    catch (const std::exception&) { declaration_source.clear(); }
    if (end < declaration_source.size() && declaration_source[end] == ';') ++end;
    if (start <= end && end <= declaration_source.size()) {
      std::string_view text(declaration_source.data() + start, end - start);
      const std::size_t cut = text.find('{');
      signature = compact_space(text.substr(0, std::min<std::size_t>(cut == std::string_view::npos ? text.size() : cut, 768)));
    }
    const std::string type = take(a, a.getTypeSpelling(a.getCursorType(cursor)));
    const std::string qname = qualified_name(c, cursor);
    const std::string parent = nearest_parent_usr(c, cursor);
    const std::string comment = take(a, a.Cursor_getRawCommentText(cursor));
    const bool body_required = is_header(rel) || kind.find("Template") != std::string::npos ||
      signature.find("constexpr") != std::string::npos || signature.find("consteval") != std::string::npos ||
      signature.find("inline") != std::string::npos || signature.find("concept") != std::string::npos;
    std::ostringstream line;
    line << "{\"record\":\"symbol\",\"id\":" << json(usr) << ",\"name\":" << json(name)
         << ",\"qualified_name\":" << json(qname) << ",\"kind\":" << json(kind)
         << ",\"file\":" << json(rel) << ",\"start\":" << start << ",\"end\":" << end
         << ",\"line\":" << rr.begin.line << ",\"column\":" << rr.begin.column
         << ",\"end_line\":" << rr.end.line << ",\"end_column\":" << rr.end.column
         << ",\"name_start\":" << loc.offset << ",\"name_end\":" << (loc.offset + name.size())
         << ",\"definition\":" << (a.isCursorDefinition(cursor) ? "true" : "false")
         << ",\"signature\":" << json(signature) << ",\"type\":" << json(type)
         << ",\"tokens\":" << token_count(c, a.getCursorExtent(cursor))
         << ",\"parent_id\":" << json(parent) << ",\"linkage\":" << a.getCursorLinkage(cursor)
         << ",\"visibility\":" << a.getCursorVisibility(cursor) << ",\"access\":" << a.getCXXAccessSpecifier(cursor)
         << ",\"public\":" << ((rel.rfind("include/", 0) == 0 || a.getCursorLinkage(cursor) == 4) ? "true" : "false")
         << ",\"body_required\":" << (body_required ? "true" : "false")
         << ",\"template\":" << (kind.find("Template") != std::string::npos ? "true" : "false")
         << ",\"inline\":" << (signature.find("inline") != std::string::npos ? "true" : "false")
         << ",\"constexpr\":" << (signature.find("constexpr") != std::string::npos ? "true" : "false")
         << ",\"cuda\":" << (((rel.size() >= 3 && rel.compare(rel.size() - 3, 3, ".cu") == 0) ||
                                  (rel.size() >= 4 && rel.compare(rel.size() - 4, 4, ".cuh") == 0) ||
                                  signature.find("__global__") != std::string::npos) ? "true" : "false")
         << ",\"macro_related\":false,\"contract\":" << json(comment) << "}";
    c.records.push_back({"S\t" + usr + "\t" + rel + "\t" + std::to_string(start), line.str()});
    c.declarations_by_name[name].push_back(usr);
    if (!parent.empty()) {
      std::ostringstream edge;
      edge << "{\"record\":\"edge\",\"type\":\"containment\",\"from\":" << json(parent)
           << ",\"to\":" << json(usr) << ",\"file\":" << json(rel) << ",\"start\":" << start << ",\"end\":" << end << "}";
      c.records.push_back({"E\tcontainment\t" + parent + "\t" + usr + "\t" + std::to_string(start), edge.str()});
    }
    CXCursor* overridden = nullptr; unsigned overridden_n = 0;
    a.getOverriddenCursors(cursor, &overridden, &overridden_n);
    for (unsigned i = 0; i < overridden_n; ++i) {
      std::string base = cursor_usr(c, overridden[i]);
      if (base.empty()) continue;
      std::ostringstream edge;
      edge << "{\"record\":\"edge\",\"type\":\"override\",\"from\":" << json(usr)
           << ",\"to\":" << json(base) << ",\"file\":" << json(rel) << ",\"start\":" << start << ",\"end\":" << end << "}";
      c.records.push_back({"E\toverride\t" + usr + "\t" + base, edge.str()});
    }
    if (overridden) a.disposeOverriddenCursors(overridden);
  }

  if (a.isReference(cursor.kind) || a.isExpression(cursor.kind) || kind == "CXXBaseSpecifier" || lower_kind == "macro expansion") {
    CXCursor target = a.getCursorReferenced(cursor);
    std::string to = cursor_usr(c, target);
    std::string from = nearest_parent_usr(c, cursor);
    if (to.empty() && lower_kind == "macro expansion") {
      auto found = c.declarations_by_name.find(name);
      if (found != c.declarations_by_name.end() && found->second.size() == 1) to = found->second.front();
    }
    if (!to.empty() && to != from) {
      if (from.empty()) from = "@" + rel;
      std::string edge_type = "reference";
      if (kind.find("CallExpr") != std::string::npos) edge_type = "call";
      else if (kind == "TypeRef" || kind == "TemplateRef") edge_type = "type_use";
      else if (kind.find("MemberRef") != std::string::npos) edge_type = "member_access";
      else if (kind == "CXXBaseSpecifier") edge_type = "inheritance";
      else if (lower_kind == "macro expansion") edge_type = "macro_expansion";
      std::ostringstream edge;
      edge << "{\"record\":\"edge\",\"type\":" << json(edge_type) << ",\"from\":" << json(from)
           << ",\"to\":" << json(to) << ",\"file\":" << json(rel) << ",\"start\":" << rr.begin.offset
           << ",\"end\":" << rr.end.offset << ",\"kind\":" << json(kind) << "}";
      c.records.push_back({"E\t" + edge_type + "\t" + from + "\t" + to + "\t" + std::to_string(rr.begin.offset), edge.str()});
    }
  }

  if (kind == "InclusionDirective") {
    CXFile inc = a.getIncludedFile(cursor);
    if (inc) {
      const std::string target = relative_to(normalize(take(a, a.getFileName(inc))), c.root);
      std::ostringstream line;
      line << "{\"record\":\"include\",\"from\":" << json(rel) << ",\"to\":" << json(target)
           << ",\"line\":" << loc.line << "}";
      c.records.push_back({"I\t" + rel + "\t" + target + "\t" + std::to_string(loc.line), line.str()});
    }
  }
  return ctxpp::clangabi::Recurse;
}

struct ParsedArgs {
  std::string command, root, file, libclang;
  unsigned start{}, end{};
  std::vector<std::string> clang_args;
};

ParsedArgs parse_args(int argc, char** argv) {
  ParsedArgs out;
  if (argc > 1) out.command = argv[1];
  bool compiler = false;
  for (int i = 2; i < argc; ++i) {
    std::string v = argv[i];
    if (v == "--") { compiler = true; continue; }
    if (compiler) { out.clang_args.push_back(v); continue; }
    auto value = [&](std::string& target) { if (++i >= argc) throw std::runtime_error("missing value for " + v); target = argv[i]; };
    if (v == "--root") value(out.root);
    else if (v == "--file") value(out.file);
    else if (v == "--libclang") value(out.libclang);
    else if (v == "--start") { std::string x; value(x); out.start = static_cast<unsigned>(std::stoul(x)); }
    else if (v == "--end") { std::string x; value(x); out.end = static_cast<unsigned>(std::stoul(x)); }
    else throw std::runtime_error("unknown argument: " + v);
  }
  return out;
}

CXTranslationUnit parse_tu(Api& a, const ParsedArgs& p, CXIndex index, const std::string* unsaved = nullptr) {
  std::vector<const char*> cargs;
  for (const std::string& s : p.clang_args) cargs.push_back(s.c_str());
  CXTranslationUnit tu{};
  constexpr unsigned detailed_preprocessing = 0x01;
  constexpr unsigned keep_going = 0x200;
  ctxpp::clangabi::CXUnsavedFile unsaved_file{p.file.c_str(), unsaved ? unsaved->data() : nullptr,
                                              unsaved ? static_cast<unsigned long>(unsaved->size()) : 0};
  int err = a.parseTranslationUnit2(index, p.file.c_str(), cargs.data(), static_cast<int>(cargs.size()),
                                    unsaved ? &unsaved_file : nullptr, unsaved ? 1U : 0U,
                                    detailed_preprocessing | keep_going, &tu);
  if (err != 0 || !tu) throw std::runtime_error("libclang parse failed with code " + std::to_string(err));
  return tu;
}

int doctor(Api& a) {
  std::cout << "{\"available\":true,\"backend\":\"libclang-runtime\",\"version\":" << json(take(a, a.getClangVersion())) << "}\n";
  return 0;
}

int scan(Api& a, const ParsedArgs& p) {
  if (p.root.empty() || p.file.empty()) throw std::runtime_error("scan requires --root and --file");
  const std::string root = normalize(p.root), file = normalize(p.file);
  const std::string source = read_file(file);
  CXIndex index = a.createIndex(0, 0);
  CXTranslationUnit tu{};
  try { tu = parse_tu(a, p, index); }
  catch (...) { a.disposeIndex(index); throw; }
  ScanContext ctx{&a, tu, root, file, source, {}, {}, {}};
  a.visitChildren(a.getTranslationUnitCursor(tu), visit, &ctx);
  a.getInclusions(tu, inclusion, &ctx);
  CXFile main_cx_file = a.getFile(tu, file.c_str());
  if (main_cx_file && !source.empty()) {
    CXSourceRange all = a.getRange(a.getLocationForOffset(tu, main_cx_file, 0),
                                   a.getLocationForOffset(tu, main_cx_file, static_cast<unsigned>(source.size())));
    CXToken* tokens = nullptr; unsigned token_n = 0; a.tokenize(tu, all, &tokens, &token_n);
    std::vector<CXCursor> cursors(token_n);
    if (tokens && token_n) a.annotateTokens(tu, tokens, token_n, cursors.data());
    for (unsigned i = 0; i < token_n; ++i) {
      CXCursor cursor = cursors[i];
      std::string token_spelling = take(a, a.getTokenSpelling(tu, tokens[i]));
      CXCursor target = a.getCursorReferenced(cursor);
      std::string to = take(a, a.getCursorUSR(target));
      if (to.empty()) {
        auto found = ctx.declarations_by_name.find(token_spelling);
        if (found == ctx.declarations_by_name.end() || found->second.size() != 1) continue;
        to = found->second.front();
        Range tr = range(a, a.getTokenExtent(tu, tokens[i]));
        std::ostringstream opaque;
        opaque << "{\"record\":\"edge\",\"type\":\"opaque_reference\",\"from\":" << json("@" + relative_to(file, root))
               << ",\"to\":" << json(to) << ",\"file\":" << json(relative_to(file, root))
               << ",\"start\":" << tr.begin.offset << ",\"end\":" << tr.end.offset << ",\"kind\":"
               << json(take(a, a.getCursorKindSpelling(cursor.kind))) << "}";
        ctx.records.push_back({"E\topaque_reference\t" + to + "\t" + std::to_string(tr.begin.offset), opaque.str()});
        continue;
      }
      if (token_spelling != take(a, a.getCursorSpelling(target))) continue;
      std::string from = nearest_parent_usr(ctx, cursor);
      if (from.empty()) from = "@" + relative_to(file, root);
      Range tr = range(a, a.getTokenExtent(tu, tokens[i]));
      std::string kind = take(a, a.getCursorKindSpelling(cursor.kind));
      std::string edge_type = kind.find("CallExpr") != std::string::npos ? "call" :
                              (kind == "TypeRef" || kind == "TemplateRef") ? "type_use" :
                              kind.find("MemberRef") != std::string::npos ? "member_access" : "reference";
      std::ostringstream edge;
      edge << "{\"record\":\"edge\",\"type\":" << json(edge_type) << ",\"from\":" << json(from)
           << ",\"to\":" << json(to) << ",\"file\":" << json(relative_to(file, root))
           << ",\"start\":" << tr.begin.offset << ",\"end\":" << tr.end.offset << ",\"kind\":" << json(kind) << "}";
      ctx.records.push_back({"E\t" + edge_type + "\t" + from + "\t" + to + "\t" + std::to_string(tr.begin.offset), edge.str()});
    }
    if (tokens) a.disposeTokens(tu, tokens, token_n);
  }
  std::sort(ctx.records.begin(), ctx.records.end(), [](const Record& x, const Record& y) { return x.key < y.key || (x.key == y.key && x.line < y.line); });
  ctx.records.erase(std::unique(ctx.records.begin(), ctx.records.end(), [](const Record& x, const Record& y) { return x.line == y.line; }), ctx.records.end());
  std::cout << "{\"record\":\"observation\",\"backend\":\"libclang-runtime\",\"file\":"
            << json(relative_to(file, root)) << ",\"diagnostics\":[";
  const unsigned nd = a.getNumDiagnostics(tu);
  for (unsigned i = 0; i < nd; ++i) {
    auto d = a.getDiagnostic(tu, i);
    if (i) std::cout << ',';
    std::cout << json(take(a, a.formatDiagnostic(d, a.defaultDiagnosticDisplayOptions())));
    a.disposeDiagnostic(d);
  }
  std::cout << "]}\n";
  for (const auto& r : ctx.records) std::cout << r.line << '\n';
  a.disposeTranslationUnit(tu); a.disposeIndex(index);
  return 0;
}

int compact(Api& a, const ParsedArgs& p) {
  if (p.file.empty() || p.end <= p.start) throw std::runtime_error("compact requires --file and a nonempty --start/--end range");
  const std::string source = read_file(p.file);
  if (p.end > source.size()) throw std::runtime_error("compact range exceeds file");
  std::string_view selected(source.data() + p.start, p.end - p.start);
  std::size_t line_start = 0;
  while (line_start < selected.size()) {
    std::size_t n = selected.find('\n', line_start); if (n == std::string_view::npos) n = selected.size();
    std::size_t i = line_start; while (i < n && (selected[i] == ' ' || selected[i] == '\t')) ++i;
    if (i < n && selected[i] == '#') throw std::runtime_error("refusing to compact a preprocessor region");
    line_start = n + 1;
  }
  CXIndex index = a.createIndex(0, 0); CXTranslationUnit tu{};
  try { tu = parse_tu(a, p, index); } catch (...) { a.disposeIndex(index); throw; }
  CXFile file = a.getFile(tu, p.file.c_str());
  if (!file) { a.disposeTranslationUnit(tu); a.disposeIndex(index); throw std::runtime_error("file not present in translation unit"); }
  CXSourceRange r = a.getRange(a.getLocationForOffset(tu, file, p.start), a.getLocationForOffset(tu, file, p.end));
  CXToken* tokens = nullptr; unsigned n = 0; a.tokenize(tu, r, &tokens, &n);
  std::string out; std::ostringstream maps; maps << '[';
  auto needs_space = [](const std::string& left, const std::string& right) {
    if (left.empty() || right.empty()) return false;
    if (left.rfind("//", 0) == 0) return true;
    const unsigned char a = static_cast<unsigned char>(left.back());
    const unsigned char b = static_cast<unsigned char>(right.front());
    const bool aw = std::isalnum(a) || a == '_';
    const bool bw = std::isalnum(b) || b == '_';
    if (aw && bw) return true;
    if ((aw && (b == '\'' || b == '"')) || ((a == '\'' || a == '"') && bw)) return true;
    if ((std::isdigit(a) && b == '.') || (a == '.' && std::isdigit(b))) return true;
    static const std::set<std::string> joined{"++", "--", "->", "->*", ".*", "<<", ">>", "<=", ">=", "==", "!=",
                                                "&&", "||", "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "::", "##", "/*", "//", "<=>"};
    std::string pair; pair.push_back(static_cast<char>(a)); pair.push_back(static_cast<char>(b));
    if (joined.count(pair)) return true;
    if (left.size() >= 2) {
      std::string triple = left.substr(left.size() - 2) + right.substr(0, 1);
      if (joined.count(triple)) return true;
    }
    return false;
  };
  std::string previous;
  std::vector<std::string> original_spellings;
  for (unsigned i = 0; i < n; ++i) {
    std::string spelling = take(a, a.getTokenSpelling(tu, tokens[i]));
    original_spellings.push_back(spelling);
    if (i && needs_space(previous, spelling)) out += previous.rfind("//", 0) == 0 ? "\n" : " ";
    const unsigned generated_start = static_cast<unsigned>(out.size());
    out += spelling;
    Range tr = range(a, a.getTokenExtent(tu, tokens[i]));
    if (i) maps << ',';
    maps << "{\"generated_start\":" << generated_start << ",\"generated_end\":" << out.size()
         << ",\"source_start\":" << tr.begin.offset << ",\"source_end\":" << tr.end.offset << "}";
    previous = spelling;
  }
  maps << ']';
  if (tokens) a.disposeTokens(tu, tokens, n);
  std::string candidate = source.substr(0, p.start) + out + source.substr(p.end);
  CXTranslationUnit check_tu = parse_tu(a, p, index, &candidate);
  CXFile check_file = a.getFile(check_tu, p.file.c_str());
  if (!check_file) {
    a.disposeTranslationUnit(check_tu); a.disposeTranslationUnit(tu); a.disposeIndex(index);
    throw std::runtime_error("re-lex verification could not resolve the edited file");
  }
  CXSourceRange check_range = a.getRange(a.getLocationForOffset(check_tu, check_file, p.start),
                                         a.getLocationForOffset(check_tu, check_file, p.start + static_cast<unsigned>(out.size())));
  CXToken* check_tokens = nullptr; unsigned check_n = 0; a.tokenize(check_tu, check_range, &check_tokens, &check_n);
  bool same = check_n == original_spellings.size();
  for (unsigned i = 0; same && i < check_n; ++i)
    same = take(a, a.getTokenSpelling(check_tu, check_tokens[i])) == original_spellings[i];
  if (check_tokens) a.disposeTokens(check_tu, check_tokens, check_n);
  a.disposeTranslationUnit(check_tu);
  if (!same) {
    a.disposeTranslationUnit(tu); a.disposeIndex(index);
    throw std::runtime_error("re-lex verification rejected compacted token adjacency");
  }
  std::cout << "{\"text\":" << json(out) << ",\"maps\":" << maps.str() << ",\"tokens\":" << n << "}\n";
  a.disposeTranslationUnit(tu); a.disposeIndex(index);
  return 0;
}

void help() {
  std::cout << "ctxpp-core 0.1\n"
               "  doctor [--libclang PATH]\n"
               "  scan --root ROOT --file FILE [--libclang PATH] -- <clang args>\n"
               "  compact --file FILE --start BYTE --end BYTE [--libclang PATH] -- <clang args>\n";
}

}  // namespace

int main(int argc, char** argv) {
  try {
    ParsedArgs p = parse_args(argc, argv);
    if (p.command.empty() || p.command == "--help" || p.command == "help") { help(); return 0; }
    Api a = ctxpp::clangabi::load_api(p.libclang.empty() ? nullptr : p.libclang.c_str());
    int result = p.command == "doctor" ? doctor(a) : p.command == "scan" ? scan(a, p) : p.command == "compact" ? compact(a, p) : 2;
    if (result == 2) std::cerr << "unknown command: " << p.command << '\n';
    ctxpp::clangabi::close_api(a); return result;
  } catch (const std::exception& e) {
    std::cerr << "ctxpp-core: " << e.what() << '\n'; return 1;
  }
}
