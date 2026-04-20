// heartbeat-dashboard — local web UI for the heartbeat automation system.
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/config"
	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/handlers"
)

const version = "0.1.0"

func main() {
	var (
		configPath  = flag.String("config", "", "Path to heartbeat.json (default: auto-detect)")
		historyPath = flag.String("history", "", "Path to history.jsonl (default: ~/heartbeat-reports/history.jsonl)")
		port        = flag.Int("port", 8765, "HTTP listen port")
		host        = flag.String("host", "127.0.0.1", "HTTP listen host")
		showVer     = flag.Bool("version", false, "Print version and exit")
	)
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, "heartbeat-dashboard — local web UI for the heartbeat automation system")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Usage:")
		fmt.Fprintln(os.Stderr, "  heartbeat-dashboard [flags]")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Flags:")
		flag.PrintDefaults()
	}
	flag.Parse()

	if *showVer {
		fmt.Printf("heartbeat-dashboard %s\n", version)
		return
	}

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("heartbeat-dashboard: %v", err)
	}

	server, err := handlers.New(cfg, *historyPath)
	if err != nil {
		log.Fatalf("heartbeat-dashboard: %v", err)
	}

	addr := fmt.Sprintf("%s:%d", *host, *port)
	fmt.Printf("heartbeat-dashboard %s listening on http://%s\n", version, addr)
	fmt.Printf("  config: %s\n", cfg.SourcePath)
	fmt.Printf("  projects: %d\n", len(cfg.Projects))
	fmt.Println("  press Ctrl+C to stop")

	if err := http.ListenAndServe(addr, server.Handler()); err != nil {
		log.Fatalf("heartbeat-dashboard: %v", err)
	}
}
