function show_settings(webix, ev, id){
    webix.ajax().get('/cicd/data/branches', {'_id': this.getSelectedItem()}).then(function(data) {
        data = data.json();
        var form = webix.ui({
            view: "window", 
            position: 'center',
            modal: true,
            head: "Settings",
            width: 550,
            body: {
                view: 'form',
                complexData: true,
                elements: [
                    { view:"textarea", name: 'note', label:"Note" },
                    // { view:"text", name: 'limit_instanes', label:"Limit Instances" },
                    { view:"combo", name: 'dump', label:"Dump", options: dumps, },
                    {
                        cols:[
                            { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                webix.ajax().post('/cicd/update/branch', values).then(function() {
                                    form.hide();
                                    // TODO optimize
                                    window.location.reload()
                                    //var list = $$('list-branches');
                                    //list.load(list.url);
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
        form.getChildViews()[1].setValues(data[0]);
        form.show();
    });
    return false;
}
