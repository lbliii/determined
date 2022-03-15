package provisioner

import (
	"crypto/tls"

	"github.com/pkg/errors"

	"github.com/determined-ai/determined/master/internal/db"
	. "github.com/determined-ai/determined/master/internal/resourcemanagers/provisioner/provisionerConfig"
	"github.com/determined-ai/determined/master/pkg/actor"
)

// Setup initializes and registers the actor for the provisioner.
func Setup(
	ctx *actor.Context,
	config *Config,
	resourcePool string,
	cert *tls.Certificate,
	db *db.PgDB,
) (*Provisioner, *actor.Ref, error) {
	ctx.Log().Info("found provisioner configuration")
	if config.AWS != nil {
		ctx.Log().Info("connecting to AWS")
	}
	if config.GCP != nil {
		ctx.Log().Info("connecting to GCP")
	}
	provisioner, err := New(resourcePool, config, cert, db)
	if err != nil {
		return nil, nil, errors.Wrap(err, "error creating provisioner")
	}
	provisionerActor, _ := ctx.ActorOf("provisioner", provisioner)
	return provisioner, provisionerActor, nil
}
