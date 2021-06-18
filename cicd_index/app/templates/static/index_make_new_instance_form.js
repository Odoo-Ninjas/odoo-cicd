function make_new_instance() {
    var branches = webix.ajax().get("/cicd/branches");
    branches.then(function(branches) {
        var form = webix.ui({
            view: "window", 
            position: 'center',
            modal: true,
            head: "Make New Instance",
            width: 550,
            body: {
                view: 'form',
                complexData: true,
                elements: [
                    { view: 'text', name: 'sitename', label: "Name", required: true },
                    { view: 'combo', name: 'branch', label: "Based On", options: branches.json(), required: true},
                    {
                        cols:[
                            {
                                view:"button", value:"OK", css:"webix_primary", click: function() { 
                                    var values = this.getParentView().getFormView().getValues();
                                    form.hide();
                                    webix.ajax().get('/cicd/make_custom_instance', {
                                        'name': values.sitename,
                                    }).then(function(data) {
                                        form.hide();
                                        webix.message('Instance created: ' + values['sitename']);
                                        update_sites();
                                    }).fail(function(data) {
                                        alert(data.statusText);
                                        console.error(data.responseText);
                                    });
                                }
                            },
                            { view:"button", value:"Cancel", click: function() {
                                form.hide();
                            }}
                        ]
                    }
                ],
                on: {
                    'onSubmit': function() {
                    },
                }
            }
        });
        form.show();
    });
}