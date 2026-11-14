package listen

import "testing"

func TestVRF(t *testing.T) {
	for _, c := range []struct {
		env  map[string]string
		want string
	}{
		{map[string]string{}, "mgmt"},
		{map[string]string{"MGMT_VRF_NAME": "oob"}, "oob"},
		{map[string]string{"MGMT_VRF_ENABLED": "no"}, ""},
		{map[string]string{"TELEMETRY_LISTEN_VRF": "none"}, ""},
		{map[string]string{"TELEMETRY_LISTEN_VRF": "RED", "MGMT_VRF_ENABLED": "no"}, "RED"},
	} {
		lookup := func(k string) (string, bool) { v, ok := c.env[k]; return v, ok }
		if got := VRF(lookup); got != c.want {
			t.Errorf("%v: got %q, want %q", c.env, got, c.want)
		}
	}
}
