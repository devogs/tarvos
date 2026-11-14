//go:build cgo

package gnmisrv

import (
	"errors"
	"fmt"

	"github.com/msteinert/pam/v2"
)

// PAM authenticates against a PAM service (/etc/pam.d/gnmi, rendered by
// aaa_configure.py like the NETCONF one: local admin, TACACS+ users, outcome recorded
// in the tarvos-aaa state).
type PAM struct {
	Service string
}

// Authenticate implements Authenticator.
func (p PAM) Authenticate(user, password, remote string) error {
	tx, err := pam.StartFunc(p.Service, user, func(style pam.Style, _ string) (string, error) {
		switch style {
		case pam.PromptEchoOff:
			return password, nil
		case pam.PromptEchoOn:
			return user, nil
		case pam.ErrorMsg, pam.TextInfo:
			return "", nil
		}
		return "", errors.New("unsupported PAM conversation")
	})
	if err != nil {
		return fmt.Errorf("pam start: %w", err)
	}
	defer func() { _ = tx.End() }()
	if remote != "" {
		_ = tx.SetItem(pam.Rhost, remote)
	}
	if err := tx.Authenticate(pam.Silent); err != nil {
		return err
	}
	return tx.AcctMgmt(pam.Silent)
}
