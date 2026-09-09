// The analyzer is the version already locked by build_runner in app/pubspec.lock;
// no upstream dependency or lock edit is needed for this build-only tool.
// ignore_for_file: depend_on_referenced_packages

// Compiler-owned declaration spans; invoked only inside a verified fresh stage.
import 'dart:convert';
import 'dart:io';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';

String? declarationName(AstNode node) => switch (node) {
      FunctionDeclaration n => n.name.lexeme,
      MethodDeclaration n => n.name.lexeme,
      ConstructorDeclaration n => n.name?.lexeme ?? '<constructor>',
      FieldDeclaration n => n.fields.variables.map((v) => v.name.lexeme).join(','),
      ClassDeclaration n => n.namePart.typeName.lexeme,
      _ => null,
    };

void main(List<String> args) {
  final spec = jsonDecode(File(args.single).readAsStringSync()) as List;
  for (final edit in spec.cast<Map<String, dynamic>>()) {
    final file = File(edit['file'] as String), content = file.readAsStringSync();
    final unit = parseString(content: content, path: file.path, throwIfDiagnostics: true).unit;
    Iterable<AstNode> nodes = unit.declarations;
    if (edit['class'] != null) {
      nodes = (unit.declarations
              .whereType<ClassDeclaration>()
              .singleWhere((n) => n.namePart.typeName.lexeme == edit['class'])
              .body as BlockClassBody)
          .members;
    }
    final replacements = <(int, int, String)>[];
    if (edit['keep'] != null) {
      final keep = (edit['keep'] as List).cast<String>().toSet();
      for (final node in nodes) {
        if (!keep.contains(declarationName(node))) replacements.add((node.offset, node.end, ''));
      }
      final actual = nodes.map(declarationName).toSet();
      if (!actual.containsAll(keep)) throw StateError('Selected source declarations changed');
    } else {
      final node = nodes.singleWhere((n) => declarationName(n) == edit['member']);
      replacements.add((node.offset, node.end, edit['source'] as String));
    }
    var updated = content;
    for (final (start, end, text) in replacements..sort((a, b) => b.$1.compareTo(a.$1))) {
      updated = updated.replaceRange(start, end, text);
    }
    parseString(content: updated, path: file.path, throwIfDiagnostics: true);
    file.writeAsStringSync(updated);
  }
}
