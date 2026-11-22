/**
 * Completion callbacks must be bound to the owner epoch (spec §6, "Owner
 * epoch"; review rounds 8 and 9).
 *
 * A callback that a component calls after an await (a sign-in confirmed,
 * an upload finished) is handed on as a JSX prop named `onSuccess`,
 * `onComplete`, `onDone` or `onSettled`. Its value must be a call to
 * `<something>.wrap(...)` or a binding traceable to one, so a completion
 * that arrives under the next owner does nothing.
 *
 * The check parses the source with the TypeScript compiler and uses its
 * type checker, so it is not fooled by spacing, by props passed in a spread
 * object literal (`{...{ onSuccess: done }}`), by computed keys or by a
 * spread of a variable. When it cannot tell (a computed key that is not a
 * literal, a spread whose type is `any`/`unknown`, a value it cannot trace),
 * it fails closed.
 *
 * Assertions never hide a value (review round 10): `as`, `satisfies`,
 * `<T>x`, `!` and parentheses are stripped and the underlying expression is
 * inspected, not its asserted type. A const binding is followed to its
 * initializer. A type is inspected member by member through unions and
 * intersections, and `any`, `unknown` or an unconstrained type parameter
 * anywhere counts as "cannot tell".
 */
import ts from "typescript";

export const COMPLETION_PROPS = new Set(["onSuccess", "onComplete", "onDone", "onSettled"]);

function skipOuter(expression: ts.Expression): ts.Expression {
  let current = expression;
  for (;;) {
    if (
      ts.isParenthesizedExpression(current) ||
      ts.isAsExpression(current) ||
      ts.isSatisfiesExpression(current) ||
      ts.isNonNullExpression(current) ||
      ts.isTypeAssertionExpression(current)
    ) {
      current = current.expression;
    } else {
      return current;
    }
  }
}

function propertyName(name: ts.PropertyName): string | null {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name)) return name.text;
  if (ts.isComputedPropertyName(name)) {
    const key = skipOuter(name.expression);
    if (ts.isStringLiteral(key) || ts.isNoSubstitutionTemplateLiteral(key)) return key.text;
  }
  return null;
}

/** `file:line: why: code` for every completion callback that is not bound to the epoch. */
export function unwrappedCompletionCallbacks(sourceFile: ts.SourceFile, checker: ts.TypeChecker, label: string = sourceFile.fileName): string[] {
  const found: string[] = [];
  const report = (node: ts.Node, why: string) => {
    const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    found.push(`${label}:${line}: ${why}: ${node.getText(sourceFile).replace(/\s+/g, " ").slice(0, 100)}`);
  };

  const declarationOf = (symbol: ts.Symbol | undefined): ts.Declaration | undefined => {
    if (!symbol) return undefined;
    const target = symbol.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(symbol) : symbol;
    return target.valueDeclaration ?? target.declarations?.[0];
  };

  /** True when `expression` is a `.wrap(...)` call, or a const binding whose initializer is (transitively). */
  const isWrapped = (expression: ts.Expression, seen = new Set<ts.Node>()): boolean => {
    const value = skipOuter(expression);
    if (ts.isCallExpression(value)) {
      const callee = skipOuter(value.expression);
      return ts.isPropertyAccessExpression(callee) && callee.name.text === "wrap";
    }
    if (ts.isIdentifier(value)) return isTraceable(declarationOf(checker.getSymbolAtLocation(value)), seen);
    return false;
  };

  const isTraceable = (declaration: ts.Declaration | undefined, seen: Set<ts.Node>): boolean => {
    if (!declaration || seen.has(declaration)) return false;
    seen.add(declaration);
    if (!ts.isVariableDeclaration(declaration) || !ts.isIdentifier(declaration.name) || !declaration.initializer) return false;
    const list = declaration.parent;
    // Only a const binding: a `let` could be reassigned to anything.
    if (!ts.isVariableDeclarationList(list) || !(list.flags & ts.NodeFlags.Const)) return false;
    return isWrapped(declaration.initializer, seen);
  };

  /**
   * Inspect a type member by member (unions, intersections, constraints of
   * type parameters). Reports each completion prop any member carries, and
   * "cannot tell" for any, unknown or an unconstrained type parameter.
   */
  const inspectType = (node: ts.Node, type: ts.Type, seen = new Set<ts.Type>()) => {
    if (seen.has(type)) return;
    seen.add(type);
    if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) {
      report(node, "spread of an untyped value (cannot tell whether it carries a completion callback)");
      return;
    }
    if (type.isUnionOrIntersection()) {
      for (const member of type.types) inspectType(node, member, seen);
      return;
    }
    if (type.flags & ts.TypeFlags.TypeParameter) {
      const constraint = checker.getBaseConstraintOfType(type);
      if (!constraint || constraint === type || constraint.flags & ts.TypeFlags.Unknown) {
        report(node, "spread of a generic value (cannot tell whether it carries a completion callback)");
        return;
      }
      inspectType(node, constraint, seen);
      return;
    }
    for (const name of COMPLETION_PROPS) {
      if (checker.getPropertyOfType(type, name)) report(node, `spread carries ${name}, which cannot be traced to .wrap(...)`);
    }
  };

  /**
   * A spread value: strip assertions to the underlying expression, follow a
   * const binding to its initializer, inspect an object literal property by
   * property, and anything else by the type of the underlying expression
   * (never the asserted type).
   */
  const checkSpreadValue = (node: ts.Node, expression: ts.Expression, seen = new Set<ts.Node>()) => {
    const value = skipOuter(expression);
    if (ts.isObjectLiteralExpression(value)) {
      checkObjectLiteral(value);
      return;
    }
    if (ts.isIdentifier(value)) {
      const declaration = declarationOf(checker.getSymbolAtLocation(value));
      if (
        declaration &&
        !seen.has(declaration) &&
        ts.isVariableDeclaration(declaration) &&
        declaration.initializer &&
        ts.isVariableDeclarationList(declaration.parent) &&
        declaration.parent.flags & ts.NodeFlags.Const
      ) {
        seen.add(declaration);
        // Both the initializer and the binding's own type must be clean.
        checkSpreadValue(node, declaration.initializer, seen);
      }
    }
    inspectType(node, checker.getTypeAtLocation(value));
  };

  const checkObjectLiteral = (object: ts.ObjectLiteralExpression) => {
    for (const property of object.properties) {
      if (ts.isSpreadAssignment(property)) {
        checkSpreadValue(property, property.expression);
        continue;
      }
      const name = property.name ? propertyName(property.name) : null;
      if (name === null) {
        report(property, "computed prop name (cannot tell whether it is a completion callback)");
        continue;
      }
      if (!COMPLETION_PROPS.has(name)) continue;
      if (ts.isPropertyAssignment(property)) {
        if (!isWrapped(property.initializer)) report(property, `${name} is not .wrap(...)ed`);
      } else if (ts.isShorthandPropertyAssignment(property)) {
        const traced = isTraceable(declarationOf(checker.getShorthandAssignmentValueSymbol(property)), new Set());
        if (!traced) report(property, `${name} is not .wrap(...)ed`);
      } else {
        report(property, `${name} is a method or accessor, not .wrap(...)ed`);
      }
    }
  };

  const visit = (node: ts.Node) => {
    if (ts.isJsxAttribute(node)) {
      const name = ts.isIdentifier(node.name) ? node.name.text : node.name.getText(sourceFile);
      if (COMPLETION_PROPS.has(name)) {
        const initializer = node.initializer;
        const expression = initializer && ts.isJsxExpression(initializer) ? initializer.expression : undefined;
        if (!expression || !isWrapped(expression)) report(node, `${name} is not .wrap(...)ed`);
      }
    } else if (ts.isJsxSpreadAttribute(node)) {
      checkSpreadValue(node, node.expression);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

/** A program over in-memory fixtures (for the check's own tests). */
export function fixtureProgram(files: Record<string, string>): ts.Program {
  const options: ts.CompilerOptions = { jsx: ts.JsxEmit.Preserve, strict: true, noEmit: true, target: ts.ScriptTarget.ES2022, noLib: true, types: [] };
  const host = ts.createCompilerHost(options);
  const original = host.getSourceFile.bind(host);
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) =>
    fileName in files
      ? ts.createSourceFile(fileName, files[fileName], languageVersion, true, ts.ScriptKind.TSX)
      : original(fileName, languageVersion, onError, shouldCreate);
  host.fileExists = (fileName) => fileName in files;
  host.readFile = (fileName) => files[fileName];
  return ts.createProgram(Object.keys(files), options, host);
}

/** The project's own program over `files`, with the package's compiler options. */
export function projectProgram(files: string[], tsconfigPath: string): ts.Program {
  const config = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, tsconfigPath.replace(/[/\\][^/\\]+$/, ""));
  return ts.createProgram(files, parsed.options);
}
