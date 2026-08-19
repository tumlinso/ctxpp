#include "ctxpp/clang_abi.hpp"

#include <dlfcn.h>

#include <array>
#include <stdexcept>
#include <string>

namespace ctxpp::clangabi {
namespace {

template <typename T>
T symbol(void* handle, const char* name) {
  void* ptr = dlsym(handle, name);
  if (!ptr) throw std::runtime_error(std::string("missing libclang symbol: ") + name);
  return reinterpret_cast<T>(ptr);
}

}  // namespace

Api load_api(const char* explicit_path) {
  std::array<const char*, 8> candidates{explicit_path, "libclang-20.so.1", "libclang-20.so", "libclang-18.so.1",
                                         "libclang-18.so", "libclang.so.18.1", "libclang.so.1", "libclang.so"};
  void* handle = nullptr;
  std::string attempts;
  for (const char* path : candidates) {
    if (!path || !*path) continue;
    handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (handle) break;
    if (!attempts.empty()) attempts += ", ";
    attempts += path;
  }
  if (!handle) throw std::runtime_error("unable to load libclang; tried: " + attempts);

  Api a;
  a.handle = handle;
#define CTXPP_LOAD(field, name) a.field = symbol<decltype(a.field)>(handle, name)
  CTXPP_LOAD(createIndex, "clang_createIndex");
  CTXPP_LOAD(disposeIndex, "clang_disposeIndex");
  CTXPP_LOAD(parseTranslationUnit2, "clang_parseTranslationUnit2");
  CTXPP_LOAD(disposeTranslationUnit, "clang_disposeTranslationUnit");
  CTXPP_LOAD(getTranslationUnitCursor, "clang_getTranslationUnitCursor");
  CTXPP_LOAD(visitChildren, "clang_visitChildren");
  CTXPP_LOAD(getCursorKindSpelling, "clang_getCursorKindSpelling");
  CTXPP_LOAD(getCursorSpelling, "clang_getCursorSpelling");
  CTXPP_LOAD(getCursorDisplayName, "clang_getCursorDisplayName");
  CTXPP_LOAD(getCursorUSR, "clang_getCursorUSR");
  CTXPP_LOAD(getCursorMangling, "clang_Cursor_getMangling");
  CTXPP_LOAD(Cursor_getRawCommentText, "clang_Cursor_getRawCommentText");
  CTXPP_LOAD(getCursorSemanticParent, "clang_getCursorSemanticParent");
  CTXPP_LOAD(getCursorReferenced, "clang_getCursorReferenced");
  CTXPP_LOAD(getCursorDefinition, "clang_getCursorDefinition");
  CTXPP_LOAD(isCursorDefinition, "clang_isCursorDefinition");
  CTXPP_LOAD(isDeclaration, "clang_isDeclaration");
  CTXPP_LOAD(isReference, "clang_isReference");
  CTXPP_LOAD(isExpression, "clang_isExpression");
  CTXPP_LOAD(Cursor_isNull, "clang_Cursor_isNull");
  CTXPP_LOAD(equalCursors, "clang_equalCursors");
  CTXPP_LOAD(getCursorLinkage, "clang_getCursorLinkage");
  CTXPP_LOAD(getCursorVisibility, "clang_getCursorVisibility");
  CTXPP_LOAD(getCXXAccessSpecifier, "clang_getCXXAccessSpecifier");
  CTXPP_LOAD(getOverriddenCursors, "clang_getOverriddenCursors");
  CTXPP_LOAD(disposeOverriddenCursors, "clang_disposeOverriddenCursors");
  CTXPP_LOAD(getCursorType, "clang_getCursorType");
  CTXPP_LOAD(getTypeSpelling, "clang_getTypeSpelling");
  CTXPP_LOAD(getCursorLocation, "clang_getCursorLocation");
  CTXPP_LOAD(getCursorExtent, "clang_getCursorExtent");
  CTXPP_LOAD(getRangeStart, "clang_getRangeStart");
  CTXPP_LOAD(getRangeEnd, "clang_getRangeEnd");
  CTXPP_LOAD(getSpellingLocation, "clang_getSpellingLocation");
  CTXPP_LOAD(getFileName, "clang_getFileName");
  CTXPP_LOAD(getFile, "clang_getFile");
  CTXPP_LOAD(getLocationForOffset, "clang_getLocationForOffset");
  CTXPP_LOAD(getRange, "clang_getRange");
  CTXPP_LOAD(getIncludedFile, "clang_getIncludedFile");
  CTXPP_LOAD(getInclusions, "clang_getInclusions");
  CTXPP_LOAD(tokenize, "clang_tokenize");
  CTXPP_LOAD(annotateTokens, "clang_annotateTokens");
  CTXPP_LOAD(disposeTokens, "clang_disposeTokens");
  CTXPP_LOAD(getTokenSpelling, "clang_getTokenSpelling");
  CTXPP_LOAD(getTokenExtent, "clang_getTokenExtent");
  CTXPP_LOAD(getNumDiagnostics, "clang_getNumDiagnostics");
  CTXPP_LOAD(getDiagnostic, "clang_getDiagnostic");
  CTXPP_LOAD(formatDiagnostic, "clang_formatDiagnostic");
  CTXPP_LOAD(defaultDiagnosticDisplayOptions, "clang_defaultDiagnosticDisplayOptions");
  CTXPP_LOAD(disposeDiagnostic, "clang_disposeDiagnostic");
  CTXPP_LOAD(getCString, "clang_getCString");
  CTXPP_LOAD(disposeString, "clang_disposeString");
  CTXPP_LOAD(getClangVersion, "clang_getClangVersion");
#undef CTXPP_LOAD
  return a;
}

void close_api(Api& api) {
  if (api.handle) dlclose(api.handle);
  api = {};
}

}  // namespace ctxpp::clangabi
