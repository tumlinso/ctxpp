#pragma once

#include <cstddef>

namespace ctxpp::clangabi {

using CXIndex = void*;
using CXTranslationUnit = void*;
using CXFile = void*;
using CXDiagnostic = void*;
using CXClientData = void*;

struct CXString { const void* data; unsigned private_flags; };
struct CXCursor { unsigned kind; int xdata; const void* data[3]; };
struct CXSourceLocation { const void* ptr_data[2]; unsigned int_data; };
struct CXSourceRange { const void* ptr_data[2]; unsigned begin_int_data; unsigned end_int_data; };
struct CXToken { unsigned int_data[4]; void* ptr_data; };
struct CXType { unsigned kind; void* data[2]; };
struct CXUnsavedFile { const char* Filename; const char* Contents; unsigned long Length; };

enum CXChildVisitResult : unsigned { Break = 0, Continue = 1, Recurse = 2 };
using CXCursorVisitor = CXChildVisitResult (*)(CXCursor, CXCursor, CXClientData);
using CXInclusionVisitor = void (*)(CXFile, CXSourceLocation*, unsigned, CXClientData);

struct Api {
  void* handle{};
  CXIndex (*createIndex)(int, int){};
  void (*disposeIndex)(CXIndex){};
  int (*parseTranslationUnit2)(CXIndex, const char*, const char* const*, int, void*, unsigned, unsigned, CXTranslationUnit*){};
  void (*disposeTranslationUnit)(CXTranslationUnit){};
  CXCursor (*getTranslationUnitCursor)(CXTranslationUnit){};
  unsigned (*visitChildren)(CXCursor, CXCursorVisitor, CXClientData){};
  CXString (*getCursorKindSpelling)(unsigned){};
  CXString (*getCursorSpelling)(CXCursor){};
  CXString (*getCursorDisplayName)(CXCursor){};
  CXString (*getCursorUSR)(CXCursor){};
  CXString (*getCursorMangling)(CXCursor){};
  CXString (*Cursor_getRawCommentText)(CXCursor){};
  CXCursor (*getCursorSemanticParent)(CXCursor){};
  CXCursor (*getCursorReferenced)(CXCursor){};
  CXCursor (*getCursorDefinition)(CXCursor){};
  unsigned (*isCursorDefinition)(CXCursor){};
  unsigned (*isDeclaration)(unsigned){};
  unsigned (*isReference)(unsigned){};
  unsigned (*isExpression)(unsigned){};
  int (*Cursor_isNull)(CXCursor){};
  unsigned (*equalCursors)(CXCursor, CXCursor){};
  unsigned (*getCursorLinkage)(CXCursor){};
  unsigned (*getCursorVisibility)(CXCursor){};
  unsigned (*getCXXAccessSpecifier)(CXCursor){};
  void (*getOverriddenCursors)(CXCursor, CXCursor**, unsigned*){};
  void (*disposeOverriddenCursors)(CXCursor*){};
  CXType (*getCursorType)(CXCursor){};
  CXString (*getTypeSpelling)(CXType){};
  CXSourceLocation (*getCursorLocation)(CXCursor){};
  CXSourceRange (*getCursorExtent)(CXCursor){};
  CXSourceLocation (*getRangeStart)(CXSourceRange){};
  CXSourceLocation (*getRangeEnd)(CXSourceRange){};
  void (*getSpellingLocation)(CXSourceLocation, CXFile*, unsigned*, unsigned*, unsigned*){};
  CXString (*getFileName)(CXFile){};
  CXFile (*getFile)(CXTranslationUnit, const char*){};
  CXSourceLocation (*getLocationForOffset)(CXTranslationUnit, CXFile, unsigned){};
  CXSourceRange (*getRange)(CXSourceLocation, CXSourceLocation){};
  CXFile (*getIncludedFile)(CXCursor){};
  void (*getInclusions)(CXTranslationUnit, CXInclusionVisitor, CXClientData){};
  void (*tokenize)(CXTranslationUnit, CXSourceRange, CXToken**, unsigned*){};
  void (*annotateTokens)(CXTranslationUnit, CXToken*, unsigned, CXCursor*){};
  void (*disposeTokens)(CXTranslationUnit, CXToken*, unsigned){};
  CXString (*getTokenSpelling)(CXTranslationUnit, CXToken){};
  CXSourceRange (*getTokenExtent)(CXTranslationUnit, CXToken){};
  unsigned (*getNumDiagnostics)(CXTranslationUnit){};
  CXDiagnostic (*getDiagnostic)(CXTranslationUnit, unsigned){};
  CXString (*formatDiagnostic)(CXDiagnostic, unsigned){};
  unsigned (*defaultDiagnosticDisplayOptions)(){};
  void (*disposeDiagnostic)(CXDiagnostic){};
  const char* (*getCString)(CXString){};
  void (*disposeString)(CXString){};
  CXString (*getClangVersion)(){};
};

Api load_api(const char* explicit_path);
void close_api(Api& api);

}  // namespace ctxpp::clangabi
