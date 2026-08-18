#include <clang/AST/ASTContext.h>
#include <clang/AST/Decl.h>
#include <clang/ASTMatchers/ASTMatchFinder.h>
#include <clang/Frontend/FrontendActions.h>
#include <clang/Index/USRGeneration.h>
#include <clang/Lex/Lexer.h>
#include <clang/Tooling/CommonOptionsParser.h>
#include <clang/Tooling/Tooling.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/raw_ostream.h>

using namespace clang;
using namespace clang::ast_matchers;

namespace {
llvm::cl::OptionCategory Category("ctxpp-libtooling options");

class DeclPrinter final : public MatchFinder::MatchCallback {
 public:
  void run(const MatchFinder::MatchResult& result) override {
    const auto* decl = result.Nodes.getNodeAs<NamedDecl>("decl");
    if (!decl || decl->isImplicit() || !result.SourceManager->isWrittenInMainFile(decl->getLocation())) return;
    llvm::SmallString<256> usr;
    if (index::generateUSRForDecl(decl, usr)) return;
    const auto& sm = *result.SourceManager;
    SourceLocation begin = sm.getSpellingLoc(decl->getBeginLoc());
    SourceLocation end = Lexer::getLocForEndOfToken(sm.getSpellingLoc(decl->getEndLoc()), 0, sm, result.Context->getLangOpts());
    llvm::outs() << "{\"record\":\"symbol-probe\",\"id\":\"" << usr << "\",\"name\":\""
                 << decl->getQualifiedNameAsString() << "\",\"start\":" << sm.getFileOffset(begin)
                 << ",\"end\":" << sm.getFileOffset(end) << "}\n";
  }
};
}  // namespace

int main(int argc, const char** argv) {
  auto options = tooling::CommonOptionsParser::create(argc, argv, Category);
  if (!options) { llvm::errs() << llvm::toString(options.takeError()) << '\n'; return 1; }
  tooling::ClangTool tool(options->getCompilations(), options->getSourcePathList());
  DeclPrinter printer;
  MatchFinder finder;
  finder.addMatcher(namedDecl(unless(isImplicit())).bind("decl"), &printer);
  return tool.run(tooling::newFrontendActionFactory(&finder).get());
}
