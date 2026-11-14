//go:build !cgo

package gnmisrv

import "errors"

// PAM is unavailable without cgo (libpam): every authentication fails.
type PAM struct {
	Service string
}

// Authenticate implements Authenticator.
func (PAM) Authenticate(string, string, string) error {
	return errors.New("built without cgo: PAM authentication unavailable")
}
