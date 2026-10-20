package dev.kaiforward.app

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        PushRegistration.enqueue(this)
        enableEdgeToEdge()
        setContent {
            val colors = if (isSystemInDarkTheme()) darkColorScheme(primary = Color(0xFF9DD5C4))
                else lightColorScheme(primary = Color(0xFF164E43), secondary = Color(0xFF52655E),
                    background = Color(0xFFF8FAF7), surface = Color(0xFFF8FAF7))
            MaterialTheme(colorScheme = colors) {
                SmsViewer(initialConversation = intent.getStringExtra("conversation"),
                    initialNumber = intent.getStringExtra("sender"), initialMessage = intent.getStringExtra("messageKey"))
            }
        }
    }
}

private fun dateTime(timestamp: Long): String = DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM, FormatStyle.SHORT)
    .withZone(ZoneId.systemDefault()).format(Instant.ofEpochMilli(timestamp))

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SmsViewer(model: MessagesViewModel = viewModel(), initialConversation: String? = null,
    initialNumber: String? = null, initialMessage: String? = null) {
    val state by model.state.collectAsStateWithLifecycle()
    val searchState by model.searchState.collectAsStateWithLifecycle()
    val threadState by model.threadState.collectAsStateWithLifecycle()
    val sending by model.sendingState.collectAsStateWithLifecycle()
    var composeOpen by rememberSaveable { mutableStateOf(false) }
    var draftNumber by rememberSaveable { mutableStateOf("") }
    var draftText by remember { mutableStateOf("") }
    var draftSlot by rememberSaveable { mutableIntStateOf(1) }
    var submittedDraft by remember { mutableStateOf<Triple<String, String, Int>?>(null) }
    LaunchedEffect(sending.accepted) {
        if (sending.accepted > 0 && submittedDraft == Triple(draftNumber, draftText, draftSlot)) draftText = ""
        submittedDraft = null
    }
    var selected by rememberSaveable { mutableStateOf(initialConversation) }
    var selectedNumber by rememberSaveable { mutableStateOf(initialNumber) }
    var selectedMessage by rememberSaveable { mutableStateOf(initialMessage) }
    var searchOpen by rememberSaveable { mutableStateOf(false) }
    var search by rememberSaveable { mutableStateOf("") }
    var contactHelp by rememberSaveable { mutableStateOf(false) }
    val context = LocalContext.current
    val notificationPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    val pushPrefs = remember { context.getSharedPreferences("push", android.content.Context.MODE_PRIVATE) }
    var offerNotifications by remember { mutableStateOf(BuildConfig.PUSH_CONFIGURED &&
        android.os.Build.VERSION.SDK_INT >= 33 && !pushPrefs.getBoolean("permissionAsked", false) &&
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) }
    if (offerNotifications) AlertDialog(
        onDismissRequest = { offerNotifications = false; pushPrefs.edit().putBoolean("permissionAsked", true).apply() },
        title = { Text("New message notifications") },
        text = { Text("Get notified when a new SMS arrives. Notifications show the sender and message text.") },
        confirmButton = { TextButton(onClick = {
            offerNotifications = false
            pushPrefs.edit().putBoolean("permissionAsked", true).apply()
            if (android.os.Build.VERSION.SDK_INT >= 33) notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }) { Text("Enable") } },
        dismissButton = { TextButton(onClick = {
            offerNotifications = false; pushPrefs.edit().putBoolean("permissionAsked", true).apply()
        }) { Text("Not now") } })
    val keyboard = LocalSoftwareKeyboardController.current
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { allowed ->
        model.resolveContacts()
        contactHelp = !allowed
    }
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) {
        model.resolveContacts(); model.setSendingActive(true)
        if (BuildConfig.PUSH_CONFIGURED) { model.refresh(); model.refreshThread() }
    }
    LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { model.setSendingActive(false) }
    val threads = state.conversations
    val inboxThread = threads.firstOrNull { it.key == selected }
    val remoteThread = if (threadState.conversation == selected) threadState else ThreadState(conversation = selected, busy = true)
    val thread = selected?.let { key -> Conversation(key, inboxThread?.number ?: selectedNumber ?: key,
        remoteThread.messages.sortedWith(compareBy<Sms> { it.timestamp }.thenBy { it.key })) }
    val threadViewer = state.copy(busy = remoteThread.busy,
        cursor = remoteThread.cursor, error = remoteThread.error,
        refreshedAt = if (remoteThread.completed) 0L else null)
    LaunchedEffect(selected, selectedMessage, state.connected) {
        model.openThread(if (state.connected) selected else null, selectedMessage)
    }
    val searching = searchOpen && selected == null && state.connected
    val inboxListState = rememberLazyListState()
    val collapseOffset = with(LocalDensity.current) { 24.dp.roundToPx() }
    val newSmsExpanded by remember(collapseOffset) {
        derivedStateOf {
            inboxListState.firstVisibleItemIndex == 0 &&
                inboxListState.firstVisibleItemScrollOffset <= collapseOffset
        }
    }
    LaunchedEffect(searchOpen, search) { model.search(if (searchOpen) search else "") }
    fun goBack() {
        when {
            selected != null -> { selected = null; selectedNumber = null; selectedMessage = null }
            else -> { keyboard?.hide(); searchOpen = false; search = "" }
        }
    }
    BackHandler(selected != null || searchOpen, onBack = ::goBack)
    Scaffold(topBar = {
        TopAppBar(title = {
            if (searching) SearchField(search, onChange = { search = it }) else Column {
                Text(if (thread != null) state.names[thread.key] ?: searchState.names[thread.key] ?: thread.number.ifBlank { "Unknown sender" } else "Messages",
                    maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.SemiBold)
                if (state.connected && thread != null) {
                    Text(thread.number,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1,
                        overflow = TextOverflow.Ellipsis)
                }
            }
        }, navigationIcon = {
            if (selected != null || searching) IconButton(onClick = ::goBack) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
            }
        }, actions = {
            if (state.connected && !searching) {
                if (selected != null) TextButton(onClick = {
                    draftNumber = thread?.number.orEmpty(); composeOpen = true
                }) { Text("Reply") }
                if (selected == null) IconButton(onClick = { searchOpen = true }) { Icon(Icons.Default.Search, "Search messages") }
            }
        })
    }, floatingActionButton = {
        if (state.connected && selected == null && !searching) {
            ExtendedFloatingActionButton(
                onClick = { draftNumber = ""; composeOpen = true },
                expanded = newSmsExpanded,
                icon = { Icon(painterResource(R.drawable.ic_chat), contentDescription = if (newSmsExpanded) null else "New SMS") },
                text = { Text("New SMS") })
        }
    }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            if (if (selected != null) remoteThread.busy else state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (state.error != null && !searching && selected == null) {
                Surface(color = MaterialTheme.colorScheme.errorContainer, modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text(state.error!!, style = MaterialTheme.typography.bodyMedium)
                        Row {
                            if (state.connected) TextButton(onClick = model::refresh, enabled = !state.busy) { Text("Retry refresh") }
                            TextButton(onClick = model::clearError) { Text("Dismiss") }
                        }
                    }
                }
            }
            if (state.connected && sending.endpoint.isNotEmpty()) {
                Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Text(sending.notice, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                    if (!sending.online) TextButton(onClick = model::reconnectSending, enabled = !sending.busy) {
                        Text("Reconnect SMS")
                    }
                }
            }
            when {
                thread != null -> key(thread.key) {
                    PullToRefreshBox(isRefreshing = remoteThread.busy,
                        onRefresh = { if (!remoteThread.busy) model.refreshThread() }, modifier = Modifier.weight(1f)) {
                        ConversationScreen(thread, threadViewer, model::moreThreadMessages, selectedMessage)
                    }
                }
                searching -> SearchScreen(search, threads, state, searchState, model::moreSearchResults,
                    onOpen = { message ->
                        keyboard?.hide()
                        selectedNumber = message.peer
                        selectedMessage = message.key
                        selected = conversationKey(message.peer)
                    }, onOpenContact = { keyboard?.hide(); selectedNumber = it.number; selectedMessage = null; selected = it.key })
                else -> PullToRefreshBox(isRefreshing = state.busy,
                    onRefresh = { if (!state.busy) model.refresh() }, modifier = Modifier.weight(1f)) {
                    Column(Modifier.fillMaxSize()) {
                        if (!state.contactsAllowed) {
                            TextButton(onClick = { permission.launch(Manifest.permission.READ_CONTACTS) },
                                modifier = Modifier.padding(horizontal = 12.dp)) { Text("Use contacts") }
                        }
                        if (threads.isEmpty()) {
                            Box(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).padding(32.dp), contentAlignment = Alignment.Center) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Text("No messages yet", style = MaterialTheme.typography.titleLarge)
                                    Spacer(Modifier.height(8.dp))
                                    Text("Messages will appear here once your KaiForward phone uploads them. Pull down to refresh and check again.", style = MaterialTheme.typography.bodyMedium)
                                }
                            }
                            HistoryFooter(state, onLoad = model::loadOlder, inbox = true)
                            if (state.connected) Spacer(Modifier.height(96.dp))
                        } else {
                            LazyColumn(Modifier.weight(1f), state = inboxListState,
                                contentPadding = PaddingValues(top = 12.dp, bottom = if (state.connected) 96.dp else 20.dp)) {
                                items(threads, key = { "conversation:${it.key}" }) { conversation ->
                                    ConversationRow(conversation, state.names[conversation.key]) { selectedNumber = conversation.number; selectedMessage = null; selected = conversation.key }
                                }
                                item(key = "history") { HistoryFooter(state, inboxListState, model::loadOlder, inbox = true) }
                            }
                        }
                    }
                }
            }
        }
    }
    if (composeOpen && state.connected) AlertDialog(
        onDismissRequest = { if (!sending.busy) composeOpen = false },
        title = { Text("Send SMS") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Sent from your KaiForward phone’s SIM.", style = MaterialTheme.typography.bodyMedium)
                OutlinedTextField(draftNumber, { draftNumber = it }, label = { Text("Recipient") }, singleLine = true,
                    enabled = !sending.busy, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
                OutlinedTextField(draftText, { draftText = it }, label = { Text("Message") }, minLines = 3, maxLines = 6,
                    enabled = !sending.busy, supportingText = {
                        val length = smsLength(draftText)
                        Text("${length.used}/${length.limit} ${length.alphabet} · one SMS segment")
                    }, isError = draftText.isNotEmpty() && !smsLength(draftText).valid)
                Row {
                    for (slot in 1..2) FilterChip(selected = draftSlot == slot,
                        onClick = { draftSlot = slot }, label = { Text("SIM $slot") }, enabled = !sending.busy,
                        modifier = Modifier.padding(end = 8.dp))
                }
                Text(sending.notice, style = MaterialTheme.typography.bodySmall)
                if (!sending.online) TextButton(onClick = model::reconnectSending, enabled = !sending.busy) { Text("Reconnect SMS") }
            }
        },
        confirmButton = { Button(onClick = { submittedDraft = Triple(draftNumber, draftText, draftSlot); model.sendSms(draftNumber, draftText, draftSlot) },
            enabled = !sending.busy && sending.online && draftSlot in sending.readySlots &&
                validSmsNumber(draftNumber) && smsLength(draftText).valid) { Text(if (sending.busy) "Sending…" else "Send SMS") } },
        dismissButton = { TextButton(onClick = { composeOpen = false }, enabled = !sending.busy) { Text("Close") } })
    if (contactHelp) AlertDialog(onDismissRequest = { contactHelp = false },
        title = { Text("Contact names are optional") },
        text = { Text("You can keep viewing phone numbers. To enable names after denying permission, allow Contacts in Android app settings. Contact data stays on this device.") },
        confirmButton = { TextButton(onClick = {
            contactHelp = false
            context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")))
        }) { Text("App settings") } },
        dismissButton = { TextButton(onClick = { contactHelp = false }) { Text("Keep numbers") } })
}

@Composable
private fun SearchField(query: String, onChange: (String) -> Unit) {
    val focus = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    LaunchedEffect(Unit) { focus.requestFocus() }
    TextField(value = query, onValueChange = { onChange(it.take(256)) },
        placeholder = { Text("Search messages") }, singleLine = true,
        modifier = Modifier.fillMaxWidth().focusRequester(focus),
        colors = TextFieldDefaults.colors(
            focusedContainerColor = Color.Transparent, unfocusedContainerColor = Color.Transparent,
            focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent),
        trailingIcon = {
            if (query.isNotEmpty()) IconButton(onClick = { onChange("") }) { Icon(Icons.Default.Close, "Clear search") }
        },
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
        keyboardActions = KeyboardActions(onSearch = { keyboard?.hide() }))
}

@Composable
internal fun SearchScreen(query: String, threads: List<Conversation>, state: ViewerState, remoteState: SearchState,
                         loadMore: () -> Unit, onOpen: (Sms) -> Unit, onOpenContact: (Conversation) -> Unit) {
    val remote = if (remoteState.query == query.trim()) remoteState else SearchState(query.trim(), busy = true)
    val contacts = remember(threads, query, state.names) {
        threads.filter { conversation ->
            state.names[conversation.key]?.contains(query.trim(), true) == true
        }
    }
    val listState = rememberLazyListState()
    LaunchedEffect(query) { listState.requestScrollToItem(0) }
    if (query.isBlank()) {
        Box(Modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
            Text("Search by contact name, phone number, or message text", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    } else {
        LazyColumn(Modifier.fillMaxSize(), state = listState, contentPadding = PaddingValues(vertical = 16.dp)) {
            item {
                Text(if (remote.busy && remote.messages.isEmpty()) "Searching all messages…"
                    else if (remote.error != null && remote.messages.isEmpty()) "Search unavailable"
                    else if (remote.messages.isEmpty()) "No matching messages" else "${remote.messages.size} matching messages",
                    Modifier.padding(horizontal = 20.dp, vertical = 12.dp), style = MaterialTheme.typography.labelLarge)
                if (remote.error != null) Text(remote.error, Modifier.padding(horizontal = 20.dp),
                    style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
            }
            if (contacts.isNotEmpty()) item {
                Text("Matching contacts", Modifier.padding(horizontal = 20.dp, vertical = 12.dp), style = MaterialTheme.typography.labelLarge)
            }
            items(contacts, key = { "contact:${it.key}" }) { conversation ->
                ConversationRow(conversation, state.names[conversation.key]) { onOpenContact(conversation) }
            }
            items(remote.messages, key = { "message:${it.key}" }) { message ->
                val key = conversationKey(message.peer)
                ConversationRow(Conversation(key, message.peer, listOf(message)), remote.names[key] ?: state.names[key]) { onOpen(message) }
            }
            item(key = "history") {
                HistoryFooter(ViewerState(busy = remote.busy, error = remote.error, cursor = remote.cursor,
                    refreshedAt = if (remote.completed) 0L else null), listState, loadMore, search = true)
            }
        }
    }
}

@Composable
private fun HistoryFooter(state: ViewerState, listState: LazyListState? = null, onLoad: () -> Unit, search: Boolean = false, inbox: Boolean = false) {
    val currentState by rememberUpdatedState(state)
    val currentLoad by rememberUpdatedState(onLoad)
    val trigger = remember(listState) { ScrollPageTrigger() }
    // Share this gate across every history view, including reversed threads.
    // Completing a page or dismissing an error is not a new scroll.
    if (listState != null) LaunchedEffect(listState) {
        snapshotFlow {
            Triple(listState.isScrollInProgress,
                listState.layoutInfo.visibleItemsInfo.any { it.key == "history" },
                currentState.cursor != null && !currentState.busy && currentState.error == null)
        }.collect { (scrolling, visible, canLoad) ->
            if (trigger.shouldLoad(scrolling, visible, canLoad)) currentLoad()
        }
    }
    Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
        when {
            state.busy -> Column(horizontalAlignment = Alignment.CenterHorizontally) {
                CircularProgressIndicator(Modifier.size(24.dp))
                Text(if (search) "Searching…" else if (inbox) "Loading conversations…" else "Loading older messages…",
                    Modifier.padding(top = 8.dp), style = MaterialTheme.typography.labelSmall)
            }
            state.cursor != null || state.error != null -> OutlinedButton(onClick = onLoad) {
                Text(if (search) { if (state.error != null) "Retry search" else "Load more results" }
                    else if (inbox) { if (state.error != null) "Retry conversations" else "Load more conversations" }
                    else if (state.error != null) "Retry older messages" else "Load older messages")
            }
            state.refreshedAt != null -> Text(if (search) "No more results" else if (inbox) "No more conversations" else "No older messages",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun ConversationRow(conversation: Conversation, name: String?, onClick: () -> Unit) {
    val title = name ?: conversation.number.ifBlank { "Unknown sender" }
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 20.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(48.dp).background(MaterialTheme.colorScheme.secondaryContainer, CircleShape), contentAlignment = Alignment.Center) {
            Text(title.take(1).uppercase(), style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSecondaryContainer)
        }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.titleMedium)
            Text((if (conversation.latest.direction == "sent") "You: " else "") + conversation.latest.body,
                maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(4.dp))
            Text(dateTime(conversation.latest.timestamp) + (conversation.latest.simSlot?.let { " · SIM $it" } ?: ""),
                style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
internal fun ConversationScreen(thread: Conversation, state: ViewerState, loadOlder: () -> Unit, selectedMessage: String? = null) {
    val listState = rememberLazyListState()
    val matchExists = selectedMessage != null && thread.messages.any { it.key == selectedMessage }
    LaunchedEffect(thread.key, selectedMessage, matchExists) {
        if (matchExists) listState.scrollToItem(thread.messages.asReversed().indexOfFirst { it.key == selectedMessage } + 1)
    }
    // Reverse layout opens at the newest message and preserves position when history arrives.
    LazyColumn(Modifier.fillMaxSize(), state = listState, reverseLayout = true,
        contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Text("${thread.messages.size} loaded messages", style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) }
        items(thread.messages.asReversed(), key = { it.key }) { message ->
            val sent = message.direction == "sent"
            Column(Modifier.fillMaxWidth(), horizontalAlignment = if (sent) Alignment.End else Alignment.Start) {
                Surface(shape = RoundedCornerShape(18.dp),
                    color = if (sent) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainerHigh,
                    modifier = Modifier.widthIn(max = 340.dp)) {
                    SelectionContainer { Text(message.body.ifEmpty { "(Empty message)" }, Modifier.padding(14.dp), style = MaterialTheme.typography.bodyLarge) }
                }
                Text((if (sent) "Sent" else "Received") + " · " + dateTime(message.timestamp) + (message.simSlot?.let { " · SIM $it" } ?: ""),
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 5.dp, start = 4.dp, end = 4.dp))
            }
        }
        if (state.error != null) item(key = "thread-error") {
            Text(state.error, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
        }
        item(key = "history") { HistoryFooter(state, listState, loadOlder) }
    }
}
