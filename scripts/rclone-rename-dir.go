package main

import (
	"context"
	"errors"
	"path"

	"github.com/rclone/rclone/fs/operations"
	"github.com/rclone/rclone/fs/rc"
)

func init() {
	// The DLL's sync/move rejects overlapping paths on case-insensitive remotes.
	rc.Add(rc.Call{
		Path:  "cloudcluster/renamedir",
		Title: "Rename a directory within its parent",
		Fn:    renameDir,
	})
}

func renameDir(ctx context.Context, in rc.Params) (rc.Params, error) {
	f, source, err := rc.GetFsAndRemote(ctx, in)
	if err != nil {
		return nil, err
	}
	target, err := in.GetString("target")
	if err != nil {
		return nil, err
	}
	if source == "" || target == "" || source == target || path.Dir(source) != path.Dir(target) {
		return nil, errors.New("directory rename requires distinct names within one parent")
	}
	if f.Features().CaseInsensitive {
		return nil, operations.DirMoveCaseInsensitive(ctx, f, source, target)
	}
	return nil, operations.DirMove(ctx, f, source, target)
}
