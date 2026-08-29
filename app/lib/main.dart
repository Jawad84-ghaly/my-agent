// Client Nova — un seul écran de discussion, pour Android/iOS/Windows.
//
// Pas de session ni de login côté serveur (voir core/api/app_channel.py) :
// l'appairage échange un code à 6 chiffres contre un jeton opaque, stocké
// localement. `/app/messages` répond sur la même requête HTTP — pas de
// notifications push, pas d'historique serveur : l'app ne connaît que les
// messages échangés depuis son dernier lancement.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

const _prefsBaseUrl = 'nova_base_url';
const _prefsToken = 'nova_device_token';

void main() {
  runApp(const NovaApp());
}

class NovaApp extends StatelessWidget {
  const NovaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Nova',
      theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
      home: const _Root(),
    );
  }
}

/// Décide au démarrage : appairage à faire, ou déjà appairé.
class _Root extends StatefulWidget {
  const _Root();

  @override
  State<_Root> createState() => _RootState();
}

class _RootState extends State<_Root> {
  late Future<(String?, String?)> _loaded;

  @override
  void initState() {
    super.initState();
    _loaded = _loadCredentials();
  }

  Future<(String?, String?)> _loadCredentials() async {
    final prefs = await SharedPreferences.getInstance();
    return (prefs.getString(_prefsBaseUrl), prefs.getString(_prefsToken));
  }

  void _onPaired() {
    setState(() => _loaded = _loadCredentials());
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<(String?, String?)>(
      future: _loaded,
      builder: (context, snapshot) {
        if (!snapshot.hasData) {
          return const Scaffold(body: Center(child: CircularProgressIndicator()));
        }
        final (baseUrl, token) = snapshot.data!;
        if (baseUrl == null || token == null) {
          return SetupPage(onPaired: _onPaired);
        }
        return ChatPage(baseUrl: baseUrl, token: token, onLoggedOut: _onPaired);
      },
    );
  }
}

/// Échange un code d'appairage (émis côté serveur via `/admin/pairing-code`,
/// ou une route équivalente propre à un déploiement) contre un jeton d'app.
class SetupPage extends StatefulWidget {
  const SetupPage({super.key, required this.onPaired});

  final VoidCallback onPaired;

  @override
  State<SetupPage> createState() => _SetupPageState();
}

class _SetupPageState extends State<SetupPage> {
  final _baseUrlController = TextEditingController(text: 'https://nova.tondomaine.fr');
  final _codeController = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _baseUrlController.dispose();
    _codeController.dispose();
    super.dispose();
  }

  Future<void> _pair() async {
    final baseUrl = _baseUrlController.text.trim().replaceAll(RegExp(r'/+$'), '');
    final code = _codeController.text.trim();
    if (baseUrl.isEmpty || code.length != 6) {
      setState(() => _error = "Adresse du serveur et code à 6 chiffres requis.");
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final uri = Uri.parse('$baseUrl/app/pair').replace(queryParameters: {'code': code});
      final response = await http.post(uri).timeout(const Duration(seconds: 15));
      if (response.statusCode != 200) {
        throw Exception(_extractDetail(response) ?? 'Code invalide ou expiré.');
      }
      final token = (jsonDecode(response.body) as Map<String, dynamic>)['token'] as String;
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_prefsBaseUrl, baseUrl);
      await prefs.setString(_prefsToken, token);
      widget.onPaired();
    } catch (e) {
      setState(() => _error = e.toString().replaceFirst('Exception: ', ''));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Appairer Nova')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              "Demande un code d'appairage à l'administrateur de ton Nova "
              "(POST /admin/pairing-code), puis saisis-le ici.",
            ),
            const SizedBox(height: 24),
            TextField(
              controller: _baseUrlController,
              decoration: const InputDecoration(labelText: 'Adresse du serveur', border: OutlineInputBorder()),
              keyboardType: TextInputType.url,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _codeController,
              decoration: const InputDecoration(labelText: 'Code à 6 chiffres', border: OutlineInputBorder()),
              keyboardType: TextInputType.number,
              maxLength: 6,
            ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
            const SizedBox(height: 12),
            FilledButton(
              onPressed: _busy ? null : _pair,
              child: _busy
                  ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Appairer'),
            ),
          ],
        ),
      ),
    );
  }
}

class _ChatMessage {
  _ChatMessage(this.text, {required this.fromUser});

  final String text;
  final bool fromUser;
}

/// L'écran de discussion : chaque envoi attend sa réponse sur la même requête
/// HTTP (`/app/messages`), pas de historique serveur ni de notifications.
class ChatPage extends StatefulWidget {
  const ChatPage({super.key, required this.baseUrl, required this.token, required this.onLoggedOut});

  final String baseUrl;
  final String token;
  final VoidCallback onLoggedOut;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final _messages = <_ChatMessage>[];
  final _inputController = TextEditingController();
  final _scrollController = ScrollController();
  bool _sending = false;

  @override
  void dispose() {
    _inputController.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _logout() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_prefsBaseUrl);
    await prefs.remove(_prefsToken);
    widget.onLoggedOut();
  }

  Future<void> _send() async {
    final text = _inputController.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() {
      _messages.add(_ChatMessage(text, fromUser: true));
      _inputController.clear();
      _sending = true;
    });
    _scrollToBottom();
    try {
      final uri = Uri.parse('${widget.baseUrl}/app/messages').replace(queryParameters: {'text': text});
      final response = await http
          .post(uri, headers: {'Authorization': 'Bearer ${widget.token}'})
          .timeout(const Duration(seconds: 60));
      if (response.statusCode == 401) {
        await _logout();
        return;
      }
      if (response.statusCode != 200) {
        throw Exception(_extractDetail(response) ?? 'Nova ne répond pas pour le moment.');
      }
      final reply = (jsonDecode(response.body) as Map<String, dynamic>)['reply'] as String;
      setState(() => _messages.add(_ChatMessage(reply, fromUser: false)));
    } catch (e) {
      setState(() => _messages.add(
            _ChatMessage('⚠️ ${e.toString().replaceFirst('Exception: ', '')}', fromUser: false),
          ));
    } finally {
      if (mounted) setState(() => _sending = false);
      _scrollToBottom();
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Nova'),
        actions: [IconButton(onPressed: _logout, icon: const Icon(Icons.logout))],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView.builder(
              controller: _scrollController,
              padding: const EdgeInsets.all(12),
              itemCount: _messages.length,
              itemBuilder: (context, index) => _Bubble(_messages[index]),
            ),
          ),
          if (_sending) const LinearProgressIndicator(),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _inputController,
                      decoration: const InputDecoration(hintText: 'Écris à Nova…', border: OutlineInputBorder()),
                      onSubmitted: (_) => _send(),
                    ),
                  ),
                  IconButton(onPressed: _send, icon: const Icon(Icons.send)),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble(this.message);

  final _ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Align(
      alignment: message.fromUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
        decoration: BoxDecoration(
          color: message.fromUser ? scheme.primaryContainer : scheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Text(message.text),
      ),
    );
  }
}

String? _extractDetail(http.Response response) {
  try {
    final body = jsonDecode(response.body);
    if (body is Map && body['detail'] is String) return body['detail'] as String;
  } catch (_) {
    // corps non-JSON : on retombe sur le message par défaut de l'appelant.
  }
  return null;
}
