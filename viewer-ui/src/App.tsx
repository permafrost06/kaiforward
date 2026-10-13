import { useEffect, useRef, useState } from "react"
import { ArrowUpDown, ChevronLeft, ChevronRight, Inbox, MessageSquare, Monitor, Moon, RefreshCw, Search, Smartphone, Sun, X } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Skeleton } from "@/components/ui/skeleton"
import { useTheme, type Theme } from "./use-theme"

const sorts = {newest:"Newest first", oldest:"Oldest first", sender_asc:"Contact A–Z", sender_desc:"Contact Z–A"} as const
type Sort = keyof typeof sorts
type Direction = "all" | "received" | "sent"
type NumberFilter = "all" | "bd_prefix"
type Message = {key:string; id:number; timestamp:number; sender:string; recipient:string; direction:"received"|"sent"; body:string}
const contact = (message:Message) => message.direction === "sent" ? message.recipient : message.sender
type Archive = {total:number; matched:number; messages:Message[]; last_received:number|null}
const format = (value:number) => value.toLocaleString()
const dateTime = (value:number) => new Date(value).toLocaleString(undefined, {year:"numeric",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})
function initialSort():Sort {
  try { const value = localStorage.getItem("kaiforward.sort"); if (value && Object.hasOwn(sorts, value)) return value as Sort } catch { /* Use newest first. */ }
  return "newest"
}

export default function App() {
  const {theme, setTheme} = useTheme()
  const [search, setSearch] = useState("")
  const [filters, setFilters] = useState({query:"", sort:initialSort(), limit:50, offset:0, direction:"all" as Direction, numberFilter:"all" as NumberFilter})
  const [data, setData] = useState<Archive|null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [refresh, setRefresh] = useState(0)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setFilters(previous => previous.query === search.trim() ? previous : {...previous,query:search.trim(),offset:0}), 250)
    return () => clearTimeout(timer)
  }, [search])
  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false
    let timedOut = false
    const timeout = setTimeout(() => { timedOut = true; controller.abort() }, 15000)
    setLoading(true); setError("")
    const query = new URLSearchParams({q:filters.query,direction:filters.direction,number_filter:filters.numberFilter,sort:filters.sort,limit:String(filters.limit),offset:String(filters.offset)})
    fetch("/api/messages?" + query, {signal:controller.signal,cache:"no-store"})
      .then(response => { if (!response.ok) throw new Error(); return response.json() as Promise<Archive> })
      .then(archive => {
        if (cancelled) return
        if (filters.offset && filters.offset >= archive.matched) { setFilters(previous => ({...previous,offset:0})); return }
        setData(archive)
      })
      .catch(() => { if (!cancelled && (!controller.signal.aborted || timedOut)) setError("Couldn’t load your messages. Try refreshing.") })
      .finally(() => { clearTimeout(timeout); if (!cancelled) setLoading(false) })
    return () => { cancelled = true; clearTimeout(timeout); controller.abort() }
  }, [filters, refresh])
  useEffect(() => {
    const timer = setInterval(() => {
      if (!filters.offset && !document.hidden && document.activeElement !== input.current && window.getSelection()?.isCollapsed) setRefresh(value => value+1)
    }, 30000)
    return () => clearInterval(timer)
  }, [filters.offset])
  useEffect(() => {
    const shortcut = (event:KeyboardEvent) => {
      if (event.key === "/" && !(event.target instanceof HTMLInputElement) && !(event.target instanceof HTMLTextAreaElement)) { event.preventDefault(); input.current?.focus() }
    }
    document.addEventListener("keydown", shortcut)
    return () => document.removeEventListener("keydown", shortcut)
  }, [])
  const changeSort = (value:Sort) => {
    setFilters(previous => ({...previous,sort:value,offset:0}))
    try { localStorage.setItem("kaiforward.sort",value) } catch { /* Sorting still works without storage. */ }
  }
  const hasFilters = !!filters.query || filters.direction !== "all" || filters.numberFilter !== "all"
  const clearFilters = () => { setSearch(""); setFilters(previous => ({...previous,query:"",direction:"all",numberFilter:"all",offset:0})) }
  const page = Math.floor(filters.offset / filters.limit)+1
  const pages = data ? Math.ceil(data.matched / filters.limit) : 0
  const ThemeIcon = theme === "system" ? Monitor : theme === "dark" ? Moon : Sun
  const changePage = (offset:number) => { setFilters(previous => ({...previous,offset})); window.scrollTo({top:0,behavior:"smooth"}) }

  return <div className="min-h-screen">
    <header className="border-b bg-card">
      <div className="mx-auto flex h-18 max-w-6xl items-center justify-between gap-4 px-5 sm:px-8">
        <a href="/" className="flex items-center gap-2.5 text-base font-semibold tracking-tight"><span className="flex size-8 items-center justify-center rounded-lg bg-foreground text-background"><MessageSquare className="size-4" /></span>KaiForward</a>
        <div className="flex items-center gap-3">
          <Badge variant="outline" className="hidden gap-1.5 font-normal text-muted-foreground sm:flex"><span className="size-1.5 rounded-full bg-emerald-500" />Local archive</Badge>
          <div className="relative"><ThemeIcon className="pointer-events-none absolute left-3 top-1/2 z-10 size-3.5 -translate-y-1/2 text-muted-foreground" /><NativeSelect value={theme} onChange={event => setTheme(event.target.value as Theme)} aria-label="Color theme" className="h-9 w-31 bg-background pl-9"><NativeSelectOption value="system">Auto</NativeSelectOption><NativeSelectOption value="light">Light</NativeSelectOption><NativeSelectOption value="dark">Dark</NativeSelectOption></NativeSelect></div>
        </div>
      </div>
    </header>
    <main className="mx-auto max-w-6xl px-5 py-8 sm:px-8 sm:py-10">
      <div className="mb-7 flex items-start justify-between gap-4">
        <div><p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground"><Smartphone className="size-3.5" />Nokia 8110 4G</p><h1 className="text-3xl font-semibold tracking-tight">Messages</h1><p className="mt-2 text-sm text-muted-foreground">Your received and sent SMSes, all in one place.</p></div>
        <Button variant="outline" className="mt-6 gap-2 bg-card" onClick={() => setRefresh(value => value+1)} disabled={loading}><RefreshCw className={"size-4 " + (loading ? "animate-spin" : "")} />Refresh</Button>
      </div>
      <div className="mb-7 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Card className="gap-0 py-0 shadow-none"><CardContent className="flex items-center justify-between p-5"><div><p className="text-xs font-medium text-muted-foreground">Backed up</p><p id="total" className="mt-1.5 text-2xl font-semibold tabular-nums">{data ? format(data.total) : "—"}<span className="ml-2 text-xs font-normal text-muted-foreground">SMSes</span></p></div><div className="flex size-10 items-center justify-center rounded-xl bg-muted"><Inbox className="size-4 text-muted-foreground" /></div></CardContent></Card>
        <Card className="gap-0 py-0 shadow-none"><CardContent className="p-5"><p className="text-xs font-medium text-muted-foreground">Last upload</p><p className="mt-2 text-sm font-medium">{data?.last_received ? dateTime(data.last_received) : "No uploads yet"}</p><p className="mt-0.5 text-xs text-muted-foreground">Time shown on this PC</p></CardContent></Card>
        <Card className="gap-0 py-0 shadow-none"><CardContent className="p-5"><p className="text-xs font-medium text-muted-foreground">Automatic refresh</p><p className="mt-2 text-sm font-medium">Every 30 seconds</p><p className="mt-0.5 text-xs text-muted-foreground">New messages appear here</p></CardContent></Card>
      </div>
      <Card className="gap-0 overflow-hidden py-0 shadow-sm">
        <div className="flex flex-col justify-between gap-3 border-b p-4 sm:p-5 lg:flex-row lg:items-center">
          <div className="relative w-full sm:max-w-sm"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input ref={input} value={search} onChange={event => setSearch(event.target.value)} placeholder="Search contact or message…" aria-label="Search contact or message" className="h-10 bg-background pl-9 pr-9" />{search ? <button onClick={() => setSearch("")} aria-label="Clear search" className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"><X className="size-4" /></button> : <kbd className="absolute right-3 top-1/2 -translate-y-1/2 rounded border px-1.5 text-[10px] text-muted-foreground">/</kbd>}</div>
          <div className="flex shrink-0 flex-wrap items-center gap-2"><NativeSelect value={filters.direction} onChange={event => setFilters(previous => ({...previous,direction:event.target.value as Direction,offset:0}))} aria-label="Message direction" className="h-10 w-36 bg-background"><NativeSelectOption value="all">All SMSes</NativeSelectOption><NativeSelectOption value="received">Received</NativeSelectOption><NativeSelectOption value="sent">Sent</NativeSelectOption></NativeSelect><NativeSelect value={filters.numberFilter} onChange={event => setFilters(previous => ({...previous,numberFilter:event.target.value as NumberFilter,offset:0}))} aria-label="Number prefix filter" className="h-10 w-40 bg-background"><NativeSelectOption value="all">All numbers</NativeSelectOption><NativeSelectOption value="bd_prefix">+880 or 01</NativeSelectOption></NativeSelect><ArrowUpDown className="size-3.5 text-muted-foreground" /><NativeSelect value={filters.sort} onChange={event => changeSort(event.target.value as Sort)} aria-label="Sort messages" className="h-10 w-44 bg-background">{Object.entries(sorts).map(([value,label]) => <NativeSelectOption key={value} value={value}>{label}</NativeSelectOption>)}</NativeSelect></div>
        </div>
        <div className="flex items-center justify-between gap-3 border-b bg-muted/35 px-5 py-3"><h2 className="text-xs font-semibold">{filters.query ? "Search results" : filters.numberFilter !== "all" ? "Filtered messages" : filters.direction === "sent" ? "Sent messages" : filters.direction === "received" ? "Received messages" : "All messages"}</h2><span id="summary" className="text-xs text-muted-foreground" role="status" aria-live="polite">{loading ? "Updating…" : data?.matched ? `${format(filters.offset+1)}–${format(filters.offset+data.messages.length)} of ${format(data.matched)}` : "0 messages"}</span></div>
        {error && <div role="alert" className="border-b bg-destructive/5 px-5 py-4 text-sm text-destructive">{error}</div>}
        <section id="messages" aria-label="SMS messages" aria-busy={loading} className="divide-y">
          {!data && loading ? Array.from({length:5},(_,index) => <div className="flex gap-3 p-5" key={index}><Skeleton className="size-9 shrink-0 rounded-lg" /><div className="w-full space-y-3"><Skeleton className="h-3 w-28" /><Skeleton className="h-3 w-3/4" /></div></div>) : data?.messages.map(message => <article key={message.key} className="message group flex gap-3.5 px-5 py-5 transition-colors hover:bg-muted/20 sm:px-6"><span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg border bg-muted/50 text-[11px] font-semibold text-muted-foreground">{contact(message) ? contact(message).replace(/^\+/,"").slice(0,2).toUpperCase() : <MessageSquare className="size-4" />}</span><div className="min-w-0 flex-1"><div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between"><div className="flex min-w-0 items-center gap-2"><h3 className="sender break-all text-sm font-semibold">{contact(message) || (message.direction === "sent" ? "Unknown recipient" : "Unknown sender")}</h3><Badge variant="outline" className="direction shrink-0 text-[10px] font-normal">{message.direction === "sent" ? "Sent" : "Received"}</Badge></div><time dateTime={new Date(message.timestamp).toISOString()} className="shrink-0 text-[11px] tabular-nums text-muted-foreground">{dateTime(message.timestamp)}</time></div><p className="body whitespace-pre-wrap break-words text-sm leading-6 text-foreground/85">{message.body}</p></div></article>)}
          {data && !data.messages.length && <div className="flex flex-col items-center px-5 py-16 text-center"><div className="mb-4 rounded-full bg-muted p-4"><Search className="size-5 text-muted-foreground" /></div><h3 className="text-sm font-semibold">{hasFilters ? "No matching messages" : "Your archive is ready"}</h3><p className="mt-1.5 max-w-sm text-sm text-muted-foreground">{hasFilters ? "Try different filters, a contact or a few words from the message." : "Received and sent SMSes will appear here as your phone syncs."}</p>{hasFilters && <Button variant="outline" className="mt-5" onClick={clearFilters}>Clear filters</Button>}</div>}
        </section>
        <div className="flex flex-col items-center justify-between gap-3 border-t bg-muted/15 px-5 py-4 sm:flex-row">
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><NativeSelect value={String(filters.limit)} onChange={event => setFilters(previous => ({...previous,limit:Number(event.target.value),offset:0}))} aria-label="Messages per page" size="sm" className="h-8 w-17 bg-card text-xs">{[25,50,100].map(value => <NativeSelectOption key={value} value={String(value)}>{value}</NativeSelectOption>)}</NativeSelect>per page</div>
          <div className="flex items-center gap-3"><span className="mr-1 text-xs tabular-nums text-muted-foreground">{pages ? `Page ${format(page)} of ${format(pages)}` : "No pages"}</span><Button variant="outline" size="icon" className="size-8 bg-card" aria-label="Previous page" disabled={!filters.offset || loading} onClick={() => changePage(Math.max(0,filters.offset-filters.limit))}><ChevronLeft className="size-4" /></Button><Button variant="outline" size="icon" className="size-8 bg-card" aria-label="Next page" disabled={!data || filters.offset+filters.limit>=data.matched || loading} onClick={() => changePage(filters.offset+filters.limit)}><ChevronRight className="size-4" /></Button></div>
        </div>
      </Card>
      <footer className="mt-6 flex flex-col items-center justify-between gap-2 text-[11px] text-muted-foreground sm:flex-row"><span className="flex items-center gap-1.5"><MessageSquare className="size-3" />Received and sent SMS archive</span><span>Messages stay on your phone. Times use your PC’s time zone.</span></footer>
    </main>
  </div>
}
